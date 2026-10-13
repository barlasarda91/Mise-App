"""prospects — lead discovery review queue.

Revision ID: 0017
Revises: 0016
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

# JSONB on Postgres, plain JSON elsewhere (keeps the migration testable on SQLite).
JSON_ = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "prospects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("business_name", sa.String(length=200), nullable=False),
        sa.Column("name_key", sa.String(length=200), nullable=False),
        sa.Column("segment", sa.String(length=32), nullable=False),
        sa.Column("region", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="new"),
        sa.Column("muted_until", sa.Date(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=True),
        sa.Column("address", sa.String(length=300), nullable=True),
        sa.Column("website", sa.String(length=300), nullable=True),
        sa.Column("instagram", sa.String(length=100), nullable=True),
        sa.Column("phone", sa.String(length=50), nullable=True),
        sa.Column("google_place_id", sa.String(length=200), nullable=True),
        sa.Column("rating", sa.Numeric(2, 1), nullable=True),
        sa.Column("review_count", sa.Integer(), nullable=True),
        sa.Column("contact_name", sa.String(length=200), nullable=True),
        sa.Column("contact_role", sa.String(length=100), nullable=True),
        sa.Column("contact_email", sa.String(length=320), nullable=True),
        sa.Column("fit_score", sa.Integer(), nullable=False),
        sa.Column("current_coffee", sa.Text(), nullable=True),
        sa.Column("why_fit", sa.Text(), nullable=False),
        sa.Column("outreach_angle", sa.Text(), nullable=False),
        sa.Column("dm_script", sa.Text(), nullable=True),
        sa.Column("walk_in_note", sa.Text(), nullable=True),
        sa.Column("sources", JSON_, nullable=True),
        sa.Column("draft_id", sa.Integer(), sa.ForeignKey("email_drafts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id", ondelete="SET NULL"), nullable=True),
        sa.Column("matched_lead_id", sa.Integer(), sa.ForeignKey("leads.id", ondelete="SET NULL"), nullable=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_prospects_name_key", "prospects", ["name_key"])
    op.create_index("ix_prospects_region", "prospects", ["region"])
    op.create_index("ix_prospects_status", "prospects", ["status"])
    op.create_index("ix_prospects_google_place_id", "prospects", ["google_place_id"])


def downgrade() -> None:
    op.drop_table("prospects")
