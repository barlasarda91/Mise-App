"""awaiting_dismissals — Done-marks on the awaiting-reply list.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "awaiting_dismissals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mailbox", sa.String(length=10), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("mailbox", "thread_id", name="uq_awaiting_dismissal"),
    )


def downgrade() -> None:
    op.drop_table("awaiting_dismissals")
