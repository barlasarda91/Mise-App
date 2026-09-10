"""email_drafts.bcc_addrs — Bcc line in the draft editor.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

JSON_ = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("email_drafts", sa.Column("bcc_addrs", JSON_, nullable=True))


def downgrade() -> None:
    op.drop_column("email_drafts", "bcc_addrs")
