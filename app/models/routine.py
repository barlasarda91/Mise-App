"""Routines, runs, run transcripts, and per-source sync cursors."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, PortableJSON, TimestampMixin, db_enum
from app.models.enums import MessageRole, RunStatus, RunTrigger, SyncSource


class Routine(TimestampMixin, Base):
    __tablename__ = "routines"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    system_prompt: Mapped[str] = mapped_column(Text)
    schedule_cron: Mapped[str | None] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(64), default="America/Los_Angeles")
    model: Mapped[str] = mapped_column(String(64), default="opus")
    enabled: Mapped[bool] = mapped_column(default=True)
    connectors: Mapped[list | None] = mapped_column(PortableJSON)

    runs: Mapped[list["Run"]] = relationship(back_populates="routine")


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_routine_started", "routine_id", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    routine_id: Mapped[int] = mapped_column(ForeignKey("routines.id"))
    status: Mapped[RunStatus] = mapped_column(db_enum(RunStatus), default=RunStatus.RUNNING)
    trigger: Mapped[RunTrigger] = mapped_column(db_enum(RunTrigger), default=RunTrigger.SCHEDULED)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # What last_run_at was when this run started (window start for its delta queries).
    last_run_at_snapshot: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    routine: Mapped[Routine] = relationship(back_populates="runs")
    messages: Mapped[list["RunMessage"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="RunMessage.id",
    )


class RunMessage(Base):
    __tablename__ = "run_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    role: Mapped[MessageRole] = mapped_column(db_enum(MessageRole))
    content: Mapped[dict | list] = mapped_column(PortableJSON)
    tool_calls: Mapped[list | None] = mapped_column(PortableJSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    run: Mapped[Run] = relationship(back_populates="messages")


class SyncState(Base):
    """Per-(source, routine) incremental cursor.

    Advancement rule (spec §6): last_run_at/cursor move only after a successful
    gather — never at run start. Overlap is safe because email-derived writes
    are idempotent by Gmail message id.
    """

    __tablename__ = "sync_state"
    __table_args__ = (UniqueConstraint("source", "routine_id", name="uq_sync_source_routine"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[SyncSource] = mapped_column(db_enum(SyncSource))
    routine_id: Mapped[int] = mapped_column(ForeignKey("routines.id"))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cursor: Mapped[dict | None] = mapped_column(PortableJSON)
