"""prospects lat/lng — the Prospects map widget.

Revision ID: 0021
Revises: 0020
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("prospects", sa.Column("lat", sa.Numeric(9, 6), nullable=True))
    op.add_column("prospects", sa.Column("lng", sa.Numeric(9, 6), nullable=True))


def downgrade() -> None:
    op.drop_column("prospects", "lng")
    op.drop_column("prospects", "lat")
