"""Run cost instrumentation — runs.usage (token counts) and runs.cost_usd.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

JSON_ = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("runs", sa.Column("usage", JSON_, nullable=True))
    op.add_column("runs", sa.Column("cost_usd", sa.Numeric(10, 4), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "cost_usd")
    op.drop_column("runs", "usage")
