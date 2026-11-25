"""prospect visit planning — the Today's visits tab.

Revision ID: 0022
Revises: 0021
"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("prospects", sa.Column("visit_on", sa.Date(), nullable=True))
    op.add_column("prospects", sa.Column("last_visited_on", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("prospects", "last_visited_on")
    op.drop_column("prospects", "visit_on")
