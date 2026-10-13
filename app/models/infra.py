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


class DisregardRule(Base):
    """Operator veto on a briefing/board item: the task is deleted and the
    routines must not re-create or surface it — matched by the task's dedup
    key and/or the counterparty's email — until the rule is removed in
    Settings."""

    __tablename__ = "disregard_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_email: Mapped[str | None] = mapped_column(String(320), index=True)
    dedup_key: Mapped[str | None] = mapped_column(String(300))
    title: Mapped[str | None] = mapped_column(String(300))  # what was disregarded, for the Settings list
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Vendor(Base):
    """A supplier Boxx buys from, classified so the briefing can tell green
    bean importers (inventory-critical green coffee purchases) from other
    vendors. Rows come from the seed list, Settings, or a routine's
    register_vendor (those start unconfirmed until Arda confirms)."""

    __tablename__ = "vendors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30), default="other")  # green_importer / other
    domains: Mapped[str] = mapped_column(String(500))  # comma-separated email domains
    confirmed: Mapped[bool] = mapped_column(default=True)
    notes: Mapped[str | None] = mapped_column(String(300))
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


class TaskRule(Base):
    """Arda's standing automation: when a run PERCEIVES the trigger condition
    (a content judgment the model makes while reading mail/calendar), it
    creates a task per this template. The inverse of DisregardRule."""

    __tablename__ = "task_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(500))  # plain-language condition
    category: Mapped[str] = mapped_column(String(40), default="governance")
    title_guidance: Mapped[str | None] = mapped_column(String(300))
    due_offset_days: Mapped[int | None] = mapped_column()
    assignee: Mapped[str | None] = mapped_column(String(100))
    # Draft rules: instead of a task, the trigger prepares an email draft
    # (never sent — Drafts review) addressed to this Arda-fixed recipient.
    draft_to: Mapped[str | None] = mapped_column(String(300))
    draft_mailbox: Mapped[str | None] = mapped_column(String(10))  # arda | hello
    # Playbook rules: one trigger spawns SEVERAL tasks. Each step is
    # {"title": str, "due_offset_days": int | None}; empty/None = single-task rule.
    steps: Mapped[list | None] = mapped_column(PortableJSON)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
