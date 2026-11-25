"""prospect notes — jotted on visits, carried into the Pipeline.

Revision ID: 0024
Revises: 0023
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("prospects", sa.Column("notes", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("prospects", "notes")
