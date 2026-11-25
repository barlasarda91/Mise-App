"""prospect questions — ask-Claude on the visits tab.

Revision ID: 0025
Revises: 0024
"""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prospect_questions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "prospect_id",
            sa.Integer(),
            sa.ForeignKey("prospects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("asked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "run_id", sa.Integer(), sa.ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_table("prospect_questions")
