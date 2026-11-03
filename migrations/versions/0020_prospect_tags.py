"""prospects county + reach_method — two separate tag dimensions
(replaces the single free-form bucket in the UI; the column stays).

Revision ID: 0020
Revises: 0019
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("prospects", sa.Column("county", sa.String(length=60), nullable=True))
    op.add_column("prospects", sa.Column("reach_method", sa.String(length=30), nullable=True))


def downgrade() -> None:
    op.drop_column("prospects", "reach_method")
    op.drop_column("prospects", "county")
