"""task_rules draft action — a rule can prepare an email draft to a fixed,
Arda-set recipient instead of creating a task.

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("task_rules", sa.Column("draft_to", sa.String(length=300), nullable=True))
    op.add_column("task_rules", sa.Column("draft_mailbox", sa.String(length=10), nullable=True))


def downgrade() -> None:
    op.drop_column("task_rules", "draft_mailbox")
    op.drop_column("task_rules", "draft_to")
