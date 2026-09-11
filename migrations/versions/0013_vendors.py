"""vendors — green bean importers vs other suppliers.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vendors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False, server_default="other"),
        sa.Column("domains", sa.String(length=500), nullable=False),
        sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("notes", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("vendors")
