"""Add production channel, handoff and audit fields to an existing V3 database.

Revision ID: 20260916_0002
Revises: 20260915_0001

The baseline intentionally creates the current metadata for a fresh installation.
Every operation here therefore checks the live schema first, which also makes the
migration safe for databases that were created from the updated baseline.
"""

from collections.abc import Iterable

import sqlalchemy as sa
from alembic import op

from app.db import models

revision = "20260916_0002"
down_revision = "20260915_0001"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _add_missing_columns(table_name: str, columns: Iterable[sa.Column[object]]) -> None:
    existing = _column_names(table_name)
    for column in columns:
        if column.name not in existing:
            op.add_column(table_name, column)


def _index_names(table_name: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "tickets" in tables:
        _add_missing_columns(
            "tickets",
            (
                sa.Column("channel", sa.String(length=32), nullable=False, server_default="web"),
                sa.Column("assigned_to", sa.String(length=200), nullable=True),
                sa.Column("sla_due_at", sa.DateTime(timezone=True), nullable=True),
                sa.Column("first_response_at", sa.DateTime(timezone=True), nullable=True),
                sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            ),
        )
        op.execute(sa.text("UPDATE tickets SET updated_at = created_at WHERE updated_at IS NULL"))
        indexes = _index_names("tickets")
        if "ix_tickets_channel" not in indexes:
            op.create_index("ix_tickets_channel", "tickets", ["channel"])
        if "ix_tickets_assigned_to" not in indexes:
            op.create_index("ix_tickets_assigned_to", "tickets", ["assigned_to"])

    if "webhook_events" in tables:
        _add_missing_columns(
            "webhook_events",
            (
                sa.Column("connector_id", sa.String(length=64), nullable=True),
                sa.Column("conversation_id", sa.String(length=36), nullable=True),
                sa.Column("message_id", sa.String(length=36), nullable=True),
                sa.Column("error_code", sa.String(length=100), nullable=True),
                sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
            ),
        )
        op.execute(
            sa.text("UPDATE webhook_events SET connector_id = provider WHERE connector_id IS NULL")
        )
        indexes = _index_names("webhook_events")
        if "ix_webhook_events_connector_id" not in indexes:
            op.create_index("ix_webhook_events_connector_id", "webhook_events", ["connector_id"])
        if "ix_webhook_events_conversation_id" not in indexes:
            op.create_index(
                "ix_webhook_events_conversation_id", "webhook_events", ["conversation_id"]
            )

    models.ChannelConversation.__table__.create(bind=bind, checkfirst=True)
    models.AuditEvent.__table__.create(bind=bind, checkfirst=True)

    if "tenant_customers" in tables:
        indexes = _index_names("tenant_customers")
        if "uq_tenant_customers_external" not in indexes:
            op.create_index(
                "uq_tenant_customers_external",
                "tenant_customers",
                ["tenant_id", "external_id"],
                unique=True,
            )


def downgrade() -> None:
    raise RuntimeError("The production schema migration is intentionally irreversible")
