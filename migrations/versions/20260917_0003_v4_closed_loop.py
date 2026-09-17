"""Add durable channel delivery and conversation automation state.

Revision ID: 20260917_0003
Revises: 20260916_0002
"""

import sqlalchemy as sa
from alembic import op

from app.db import models

revision = "20260917_0003"
down_revision = "20260916_0002"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _index_names(table_name: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "conversations" in tables:
        columns = _column_names("conversations")
        if "automation_state" not in columns:
            op.add_column(
                "conversations",
                sa.Column(
                    "automation_state",
                    sa.String(length=24),
                    nullable=False,
                    server_default="auto",
                ),
            )
        if "automation_updated_at" not in columns:
            op.add_column(
                "conversations",
                sa.Column(
                    "automation_updated_at",
                    sa.DateTime(timezone=True),
                    nullable=False,
                    server_default=sa.func.now(),
                ),
            )
        indexes = _index_names("conversations")
        if "ix_conversations_automation_state" not in indexes:
            op.create_index(
                "ix_conversations_automation_state", "conversations", ["automation_state"]
            )

    models.OutboundMessage.__table__.create(bind=bind, checkfirst=True)
    models.CustomerChannelIdentity.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    raise RuntimeError("The V4 production migration is intentionally irreversible")
