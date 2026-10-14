"""prospects.discovered_via — which search/channel surfaced each prospect,
the tuning signal for weekly discovery analytics.

Revision ID: 0018
Revises: 0017
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("prospects", sa.Column("discovered_via", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("prospects", "discovered_via")
