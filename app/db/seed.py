from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.core.config import Settings
from app.db.database import Database
from app.db.models import (
    ConnectorDefinition,
    Customer,
    DomainPackRevision,
    Order,
    Tenant,
    TenantCustomer,
)
from app.domain.registry import DomainPackRegistry


async def seed_demo_data(database: Database, settings: Settings | None = None) -> None:
    async with database.sessions() as session:
        settings = settings or Settings()
        now = datetime.now(UTC)
        tenant = await session.get(Tenant, settings.default_tenant_id)
        if tenant is None:
            tenant = Tenant(
                id=settings.default_tenant_id,
                slug=settings.default_tenant_slug,
                name="SupportPilot Demo Store",
            )
            session.add(tenant)
        customers = [
            Customer(id="demo-001", name="林小满", email="demo@example.com"),
            Customer(id="demo-002", name="陈安", email="chen@example.com"),
        ]
        for customer in customers:
            if await session.get(Customer, customer.id) is None:
                session.add(customer)
        await session.flush()
        for customer in customers:
            link = await session.scalar(
                select(TenantCustomer).where(
                    TenantCustomer.tenant_id == settings.default_tenant_id,
                    TenantCustomer.customer_id == customer.id,
                )
            )
            if link is None:
                session.add(
                    TenantCustomer(
                        tenant_id=settings.default_tenant_id,
                        customer_id=customer.id,
                        external_id=customer.id,
                    )
                )

        orders = [
            Order(
                id="ORD-1001",
                customer_id="demo-001",
                status="delivered",
                total_amount=Decimal("399.00"),
                product_name="云感降噪耳机 Pro",
                tracking_number="SF1234567890",
                created_at=now - timedelta(days=8),
                delivered_at=now - timedelta(days=3),
            ),
            Order(
                id="ORD-1002",
                customer_id="demo-001",
                status="shipped",
                total_amount=Decimal("129.00"),
                product_name="磁吸桌面充电座",
                tracking_number="YT9876543210",
                created_at=now - timedelta(days=2),
                delivered_at=None,
            ),
            Order(
                id="ORD-1003",
                customer_id="demo-001",
                status="delivered",
                total_amount=Decimal("79.00"),
                product_name="旅行收纳线材包",
                tracking_number="ZT1357924680",
                created_at=now - timedelta(days=50),
                delivered_at=now - timedelta(days=45),
            ),
            Order(
                id="ORD-2001",
                customer_id="demo-002",
                status="processing",
                total_amount=Decimal("699.00"),
                product_name="拾光机械键盘",
                tracking_number=None,
                created_at=now - timedelta(hours=10),
                delivered_at=None,
            ),
        ]
        for order in orders:
            if await session.get(Order, order.id) is None:
                session.add(order)
        pack = DomainPackRegistry(settings.domain_pack_dir).get(settings.default_domain_pack)
        existing_pack = await session.scalar(
            select(DomainPackRevision).where(
                DomainPackRevision.tenant_id == settings.default_tenant_id,
                DomainPackRevision.slug == pack.slug,
                DomainPackRevision.status == "live",
            )
        )
        if existing_pack is None:
            session.add(
                DomainPackRevision(
                    tenant_id=settings.default_tenant_id,
                    slug=pack.slug,
                    version=1,
                    status="live",
                    config_json=pack.model_dump_json(),
                    checksum=pack.checksum(),
                    created_by="development-seed",
                    published_at=now,
                )
            )
        mock_connector = await session.scalar(
            select(ConnectorDefinition).where(
                ConnectorDefinition.tenant_id == settings.default_tenant_id,
                ConnectorDefinition.name == "sandbox-commerce",
            )
        )
        if mock_connector is None:
            session.add(
                ConnectorDefinition(
                    tenant_id=settings.default_tenant_id,
                    name="sandbox-commerce",
                    provider="mock",
                    version=1,
                    status="live",
                    capabilities_json=json.dumps(
                        [
                            "get_order",
                            "list_orders",
                            "search_products",
                            "check_inventory",
                            "create_return",
                            "exchange_item",
                            "cancel_order",
                            "change_address",
                            "handoff",
                        ]
                    ),
                    config_json="{}",
                )
            )
        await session.commit()
