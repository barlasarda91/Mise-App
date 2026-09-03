"""Email drafts — composed in-app, saved as native Gmail drafts. Sending is
operator-only: the Drafts UI's Send button (which sets sent_at); the model
tool layer has no send capability."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PortableJSON, TimestampMixin, db_enum
from app.models.enums import DraftStatus, FromMailbox


class EmailDraft(TimestampMixin, Base):
    __tablename__ = "email_drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    subject: Mapped[str] = mapped_column(String(500), default="")
    body: Mapped[str | None] = mapped_column(Text)
    # From, To, and Cc are editable in the draft UI before saving to Gmail.
    to_addrs: Mapped[list | None] = mapped_column(PortableJSON)
    cc_addrs: Mapped[list | None] = mapped_column(PortableJSON)
    from_mailbox: Mapped[FromMailbox] = mapped_column(db_enum(FromMailbox))
    # Set when the draft replies to an existing conversation: the Gmail draft
    # is created on that thread with reply headers, not as a fresh compose.
    gmail_thread_id: Mapped[str | None] = mapped_column(String(128))
    related_lead_id: Mapped[int | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL")
    )
    related_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL")
    )
    gmail_draft_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[DraftStatus] = mapped_column(
        db_enum(DraftStatus), default=DraftStatus.DRAFTING
    )
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    # Set when Arda sends from the hub (operator-initiated only).
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
