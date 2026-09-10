"""Disregard rules — operator vetoes on briefing/board items.

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "disregard_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("contact_email", sa.String(320), nullable=True),
        sa.Column("dedup_key", sa.String(300), nullable=True),
        sa.Column("title", sa.String(300), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_disregard_rules_contact_email", "disregard_rules", ["contact_email"])


def downgrade() -> None:
    op.drop_index("ix_disregard_rules_contact_email", table_name="disregard_rules")
    op.drop_table("disregard_rules")
