from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app.actions.service import ActionService
from app.agent.graph import SupportGraph
from app.api.operations import create_operations_router
from app.api.platform import create_platform_router
from app.api.router import create_router
from app.connectors.credentials import EnvironmentCredentialResolver
from app.connectors.registry import ProviderRegistry
from app.core.config import ROOT_DIR, Settings, get_settings
from app.core.coordination import RedisCoordinator
from app.core.idempotency import IdempotencyCoordinator
from app.core.logging import configure_logging
from app.core.telemetry import configure_telemetry
from app.db.database import Database
from app.db.seed import seed_demo_data
from app.domain.registry import DomainPackRegistry
from app.knowledge import KnowledgeBase
from app.middleware import (
    AuditMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    RequestSizeLimitMiddleware,
)
from app.schemas import HealthResponse
from app.service import SupportService
from app.version import __version__

STATIC_DIR = ROOT_DIR / "app" / "static"
SCHEMA_REVISION = "20260916_0002"


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    application_settings = settings or get_settings()
    application_settings.validate_runtime()
    configure_logging(application_settings.log_level)
    application_database = database or Database(application_settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not application_settings.is_production:
            await application_database.create_schema()
            await seed_demo_data(application_database, application_settings)
        else:
            await application_database.verify_revision(SCHEMA_REVISION)
        knowledge_base = KnowledgeBase(
            application_settings.knowledge_base_path,
            application_settings.catalog_path,
            include_bundled=not application_settings.is_production,
        )
        app.state.database = application_database
        app.state.settings = application_settings
        app.state.domain_registry = DomainPackRegistry(application_settings.domain_pack_dir)
        app.state.provider_registry = ProviderRegistry(EnvironmentCredentialResolver())
        app.state.action_service = ActionService(application_settings, app.state.provider_registry)
        app.state.coordinator = None
        if application_settings.redis_url:
            coordinator = RedisCoordinator(application_settings.redis_url)
            if not await coordinator.ready():
                raise RuntimeError("Redis coordination service is unavailable")
            app.state.coordinator = coordinator
        app.state.idempotency = IdempotencyCoordinator(app.state.coordinator)
        app.state.support_service = SupportService(
            SupportGraph(application_settings, knowledge_base),
            application_settings,
            app.state.action_service,
        )
        yield
        if app.state.coordinator:
            await app.state.coordinator.close()
        await application_database.dispose()

    app = FastAPI(
        title=application_settings.app_name,
        version=__version__,
        docs_url="/docs" if application_settings.enable_api_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if application_settings.enable_api_docs else None,
        description=(
            "Multi-tenant hybrid e-commerce support platform with deterministic workflows, "
            "bounded agents, versioned Domain Packs and confirmed commerce actions."
        ),
        lifespan=lifespan,
    )
    configure_telemetry(app, application_settings)
    app.add_middleware(
        RequestSizeLimitMiddleware, max_bytes=application_settings.max_request_body_bytes
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(AuditMiddleware)
    app.add_middleware(
        RateLimitMiddleware, requests_per_minute=application_settings.rate_limit_per_minute
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=application_settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-API-Key",
            "X-Admin-Key",
            "Idempotency-Key",
            "X-Webhook-Signature",
            "X-Webhook-Timestamp",
        ],
    )
    app.include_router(create_router(application_settings))
    app.include_router(create_operations_router(application_settings))
    app.include_router(create_platform_router(application_settings))
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/admin", include_in_schema=False)
    async def admin_console() -> FileResponse:
        return FileResponse(STATIC_DIR / "admin.html")

    @app.get("/health/live", response_model=HealthResponse, tags=["health"])
    async def live() -> HealthResponse:
        return HealthResponse(
            status="ok", llm_mode="openai" if application_settings.llm_available else "offline"
        )

    @app.get("/health/ready", response_model=HealthResponse, tags=["health"])
    async def ready(request: Request) -> HealthResponse:
        try:
            async with request.app.state.database.sessions() as session:
                await session.execute(text("SELECT 1"))
            coordinator = getattr(request.app.state, "coordinator", None)
            if application_settings.is_production and (
                coordinator is None or not await coordinator.ready()
            ):
                raise RuntimeError("Redis is not ready")
            status_value = "ok"
        except Exception:
            status_value = "degraded"
        return HealthResponse(
            status=status_value,
            llm_mode="openai" if application_settings.llm_available else "offline",
        )

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
