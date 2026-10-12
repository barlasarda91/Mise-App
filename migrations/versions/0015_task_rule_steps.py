"""task_rules.steps — multi-task playbooks (one trigger, several tasks).

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("task_rules", sa.Column("steps", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("task_rules", "steps")
