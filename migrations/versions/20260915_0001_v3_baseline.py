"""Create the complete V3 schema on a new or existing database.

Revision ID: 20260915_0001
Revises:
"""

from alembic import op

from app.db.database import Base
from app.db import models  # noqa: F401

revision = "20260915_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    raise RuntimeError("The V3 baseline is intentionally irreversible; restore a database backup")
