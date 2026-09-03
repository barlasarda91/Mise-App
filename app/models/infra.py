"""Dedup ledger for external mutations, app KV state, and the single-user row."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PortableJSON, db_enum
from app.models.enums import MutationKind


class AppState(Base):
    """Small KV store for durable app state (e.g. the rotating QuickBooks
    refresh token — Railway env vars can't be updated by the app)."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(PortableJSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ExternalMutation(Base):
    """Every external mutation (calendar event, Gmail draft, routine-created
    task) records a dedup key here first; re-runs update instead of re-create.

    Key conventions (spec §4): calendar -> (lead_id, requested_date, purpose);
    gmail_draft -> (lead_or_task_id, draft_purpose, run_id); task -> a stable
    natural key derived from the source (e.g. gmail_msg_id, qbo_invoice_id).
    """

    __tablename__ = "external_mutations"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[MutationKind] = mapped_column(db_enum(MutationKind))
    dedup_key: Mapped[str] = mapped_column(String(300), unique=True)
    external_id: Mapped[str | None] = mapped_column(String(200))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MutedSender(Base):
    """Senders marked unimportant — hidden from the Inbox tab and its count."""

    __tablename__ = "muted_senders"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class User(Base):
    """Single row for v1 (the login gate currently checks APP_PASSWORD; this
    table is the forward path for a DB-backed credential)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
