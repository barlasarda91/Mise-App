"""Local mail index — header cache behind the awaiting-reply memory.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mail_index",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mailbox", sa.String(10), nullable=False),
        sa.Column("gmail_msg_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(64), nullable=True),
        sa.Column("from_addr", sa.String(320), nullable=True),
        sa.Column("from_name", sa.String(200), nullable=True),
        sa.Column("to_addrs", sa.String(1000), nullable=True),
        sa.Column("subject", sa.String(500), nullable=True),
        sa.Column("snippet", sa.String(300), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_outbound", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("mailbox", "gmail_msg_id", name="uq_mail_index_msg"),
    )
    op.create_index("ix_mail_index_thread", "mail_index", ["mailbox", "thread_id"])
    op.create_index("ix_mail_index_sent_at", "mail_index", ["sent_at"])
    op.create_index("ix_mail_index_from_addr", "mail_index", ["from_addr"])


def downgrade() -> None:
    op.drop_index("ix_mail_index_from_addr", table_name="mail_index")
    op.drop_index("ix_mail_index_sent_at", table_name="mail_index")
    op.drop_index("ix_mail_index_thread", table_name="mail_index")
    op.drop_table("mail_index")
