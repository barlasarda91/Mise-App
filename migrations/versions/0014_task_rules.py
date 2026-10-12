"""task_rules — Arda's standing when-X-create-task automations.

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trigger", sa.String(length=500), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False, server_default="governance"),
        sa.Column("title_guidance", sa.String(length=300), nullable=True),
        sa.Column("due_offset_days", sa.Integer(), nullable=True),
        sa.Column("assignee", sa.String(length=100), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("task_rules")
