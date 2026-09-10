"""Local mail index: header-level cache of both mailboxes' recent mail.

One row per Gmail message (headers + snippet, never bodies — those stay in
Gmail and are fetched on demand). Built once by the 90-day backfill sweep,
kept current incrementally before each run. This is what lets "threads
awaiting a Boxx reply" be a database query instead of a model-driven Gmail
search that can miss things.
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MailMessage(Base):
    __tablename__ = "mail_index"
    __table_args__ = (
        UniqueConstraint("mailbox", "gmail_msg_id", name="uq_mail_index_msg"),
        Index("ix_mail_index_thread", "mailbox", "thread_id"),
        Index("ix_mail_index_sent_at", "sent_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox: Mapped[str] = mapped_column(String(10))  # arda / hello
    gmail_msg_id: Mapped[str] = mapped_column(String(64))
    thread_id: Mapped[str | None] = mapped_column(String(64))
    from_addr: Mapped[str | None] = mapped_column(String(320), index=True)
    from_name: Mapped[str | None] = mapped_column(String(200))
    to_addrs: Mapped[str | None] = mapped_column(String(1000))
    subject: Mapped[str | None] = mapped_column(String(500))
    snippet: Mapped[str | None] = mapped_column(String(300))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_outbound: Mapped[bool] = mapped_column(default=False)  # sent by a boxxcoffee.com address
    is_bulk: Mapped[bool] = mapped_column(default=False)  # List-Unsubscribe / Precedence: bulk
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
