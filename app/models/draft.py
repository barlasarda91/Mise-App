"""Email drafts — composed in-app, saved as native Gmail drafts. Sending is
operator-only: the Drafts UI's Send button (which sets sent_at); the model
tool layer has no send capability."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, PortableJSON, TimestampMixin, db_enum
from app.models.enums import DraftStatus, FromMailbox


class StoredFile(Base):
    """Uploaded file (attachment payloads; pricelists live here with
    in_library=True for reuse). Bytes are stored in Postgres."""

    __tablename__ = "files"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(100))
    size: Mapped[int] = mapped_column()
    data: Mapped[bytes] = mapped_column(LargeBinary)
    in_library: Mapped[bool] = mapped_column(default=False)
    label: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DraftAttachment(Base):
    __tablename__ = "draft_attachments"
    __table_args__ = (UniqueConstraint("draft_id", "file_id", name="uq_draft_attachment"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(
        ForeignKey("email_drafts.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    file: Mapped[StoredFile] = relationship()


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
