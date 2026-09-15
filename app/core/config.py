from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "ShopSage Support Agent"
    app_env: str = "development"
    app_api_key: str | None = None
    admin_api_key: str | None = None
    auth_mode: str = "development"
    jwt_secret: str | None = None
    jwt_jwks_url: str | None = None
    jwt_issuer: str = "shopsage"
    jwt_audience: str = "shopsage-api"
    jwt_clock_skew_seconds: int = Field(default=30, ge=0, le=300)
    default_tenant_id: str = "tenant-demo"
    default_tenant_slug: str = "demo-store"
    default_domain_pack: str = "general-commerce"
    database_url: str = "sqlite+aiosqlite:///./data/support.db"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"
    llm_enabled: bool = True
    log_level: str = "INFO"
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:8000", "http://127.0.0.1:8000"]
    )
    rate_limit_per_minute: int = Field(default=60, ge=1, le=10_000)
    max_agent_steps: int = Field(default=4, ge=1, le=8)
    max_message_chars: int = Field(default=4_000, ge=100, le=20_000)
    max_request_body_bytes: int = Field(default=2_000_000, ge=16_384, le=10_000_000)
    expose_debug_trace: bool = False
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "shopsage-support-agent"
    knowledge_base_path: Path = ROOT_DIR / "data" / "knowledge_base.json"
    catalog_path: Path = ROOT_DIR / "data" / "catalog.json"
    domain_pack_dir: Path = ROOT_DIR / "domain_packs"
    redis_url: str | None = None
    connector_timeout_seconds: float = Field(default=10.0, ge=0.1, le=60)
    connector_max_retries: int = Field(default=2, ge=0, le=5)
    action_max_attempts: int = Field(default=3, ge=1, le=10)
    webhook_tolerance_seconds: int = Field(default=300, ge=30, le=3600)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def llm_available(self) -> bool:
        return bool(self.llm_enabled and self.openai_api_key)

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    def validate_runtime(self) -> None:
        """Reject insecure production boot before the server accepts traffic."""

        if not self.is_production:
            return
        errors: list[str] = []
        if not self.database_url.startswith("postgresql+"):
            errors.append("DATABASE_URL must use PostgreSQL in production")
        if self.auth_mode != "jwt":
            errors.append("AUTH_MODE must be jwt in production")
        valid_secret = bool(self.jwt_secret and len(self.jwt_secret) >= 32)
        valid_jwks = bool(self.jwt_jwks_url and self.jwt_jwks_url.startswith("https://"))
        if not valid_secret and not valid_jwks:
            errors.append("configure HTTPS JWT_JWKS_URL or a JWT_SECRET of at least 32 characters")
        if self.app_api_key or self.admin_api_key:
            errors.append("legacy API keys must be disabled in production")
        if not self.redis_url:
            errors.append("REDIS_URL is required for production coordination")
        if any(origin == "*" for origin in self.cors_origins):
            errors.append("wildcard CORS origins are forbidden in production")
        if errors:
            raise RuntimeError("Unsafe production configuration: " + "; ".join(errors))


@lru_cache
def get_settings() -> Settings:
    return Settings()
