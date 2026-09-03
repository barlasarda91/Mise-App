"""Draft attachments + reusable file library (pricelists etc.).

Files live in Postgres — Railway's filesystem is ephemeral.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
NOW = sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    op.create_table(
        "files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=True),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("in_library", sa.Boolean(), nullable=False),
        sa.Column("label", sa.String(100), nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
    )
    op.create_table(
        "draft_attachments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "draft_id",
            sa.Integer(),
            sa.ForeignKey("email_drafts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("file_id", sa.Integer(), sa.ForeignKey("files.id"), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.UniqueConstraint("draft_id", "file_id", name="uq_draft_attachment"),
    )
    op.create_index("ix_draft_attachments_draft_id", "draft_attachments", ["draft_id"])


def downgrade() -> None:
    op.drop_table("draft_attachments")
    op.drop_table("files")
