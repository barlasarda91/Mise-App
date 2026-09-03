"""app_state: small KV store — first use: QuickBooks' rotating refresh token
(env var is bootstrap seed only; the live token must persist, spec §5).

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

JSON_ = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "app_state",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", JSON_, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def downgrade() -> None:
    op.drop_table("app_state")
