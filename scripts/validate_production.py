from __future__ import annotations

import json
from pathlib import Path

from app.core.config import Settings
from app.domain.registry import DomainPackRegistry

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    settings = Settings(
        app_env="production",
        auth_mode="jwt",
        enforce_tenant_membership=True,
        enable_api_docs=False,
        database_url="postgresql+asyncpg://validator:validator@db/shopsage",
        redis_url="redis://:validator@redis:6379/0",
        jwt_secret="validation-only-secret-that-is-over-32-characters",
        cors_origins=["https://support.example.com"],
        app_api_key=None,
        admin_api_key=None,
    )
    settings.validate_runtime()
    packs = DomainPackRegistry(settings.domain_pack_dir).list()
    if len(packs) < 7:
        raise RuntimeError("All seven reviewed Domain Pack templates are required")
    lock = ROOT / "requirements.lock"
    if not lock.exists() or "fastapi==" not in lock.read_text(encoding="utf-8"):
        raise RuntimeError("Production dependency lock is missing or invalid")
    compose = (ROOT / "compose.production.yaml").read_text(encoding="utf-8")
    required = ["SHOPSAGE_IMAGE", "POSTGRES_IMAGE", "REDIS_IMAGE", "CADDY_IMAGE"]
    missing = [item for item in required if item not in compose]
    if missing:
        raise RuntimeError(f"Production compose omits immutable image settings: {missing}")
    print(
        json.dumps(
            {
                "status": "valid",
                "domain_packs": [pack.slug for pack in packs],
                "production_auth": settings.auth_mode,
                "database": settings.database_url.split(":", 1)[0],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
