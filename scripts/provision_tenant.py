from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.config import Settings
from app.db.database import Database
from app.db.models import DomainPackRevision, Tenant, TenantMember
from app.domain.registry import DomainPackRegistry


async def provision(args: argparse.Namespace) -> None:
    settings = Settings()
    database = Database(settings.database_url)
    registry = DomainPackRegistry(settings.domain_pack_dir)
    pack = registry.get(args.domain_pack)
    async with database.sessions() as session:
        existing = await session.get(Tenant, args.tenant_id)
        if existing:
            raise RuntimeError(f"tenant already exists: {args.tenant_id}")
        duplicate_slug = await session.scalar(select(Tenant).where(Tenant.slug == args.slug))
        if duplicate_slug:
            raise RuntimeError(f"tenant slug already exists: {args.slug}")
        tenant = Tenant(id=args.tenant_id, slug=args.slug, name=args.name)
        session.add(tenant)
        await session.flush()
        status = "live" if args.publish else "draft"
        session.add(
            DomainPackRevision(
                tenant_id=tenant.id,
                slug=pack.slug,
                version=1,
                status=status,
                config_json=pack.model_dump_json(),
                checksum=pack.checksum(),
                created_by=args.created_by,
                published_at=datetime.now(UTC) if args.publish else None,
            )
        )
        session.add(
            TenantMember(
                tenant_id=tenant.id,
                subject=args.created_by,
                role="admin",
            )
        )
        await session.commit()
    await database.dispose()
    print(f"Provisioned tenant {args.tenant_id} with {pack.slug} ({status})")


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision one ShopSage tenant")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--domain-pack", default="general-commerce")
    parser.add_argument("--created-by", required=True)
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Explicitly publish the reviewed template; otherwise create a Draft",
    )
    asyncio.run(provision(parser.parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
