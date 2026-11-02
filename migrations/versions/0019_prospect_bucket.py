"""prospects.bucket — Arda's own categories on the Prospects tab.

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("prospects", sa.Column("bucket", sa.String(length=60), nullable=True))


def downgrade() -> None:
    op.drop_column("prospects", "bucket")
