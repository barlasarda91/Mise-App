"""Wholesale pipeline: leads + timestamped activity log."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, Numeric, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, PortableJSON, TimestampMixin, db_enum
from app.models.enums import (
    ActivitySource,
    LeadActivityType,
    LeadFormat,
    LeadStage,
    ProjectedUnit,
)


class Lead(TimestampMixin, Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_name: Mapped[str] = mapped_column(String(200))
    contact_name: Mapped[str | None] = mapped_column(String(200))
    contact_email: Mapped[str | None] = mapped_column(String(320), index=True)
    contact_phone: Mapped[str | None] = mapped_column(String(50))
    location: Mapped[str | None] = mapped_column(String(200))
    lead_source: Mapped[str | None] = mapped_column(String(100))
    format: Mapped[LeadFormat | None] = mapped_column(db_enum(LeadFormat))
    # JSON list of CoffeeProgram values (multi-select).
    coffee_program: Mapped[list | None] = mapped_column(PortableJSON)
    projected_weekly_consumption: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    projected_unit: Mapped[ProjectedUnit | None] = mapped_column(db_enum(ProjectedUnit))
    stage: Mapped[LeadStage] = mapped_column(
        db_enum(LeadStage), default=LeadStage.NEW, index=True
    )
    stage_since: Mapped[date | None] = mapped_column(Date)
    # Cadence timers anchor here; derived from the latest outbound lead_activity.
    last_confirmed_action: Mapped[date | None] = mapped_column(Date)
    # A found action awaiting Arda's one-click Confirm before the idle timer
    # resets (hold-and-confirm, spec §7.1). Shape: {activity: {...}, found_by_run_id}.
    pending_confirmation: Mapped[dict | None] = mapped_column(PortableJSON)
    loss_reason: Mapped[str | None] = mapped_column(Text)
    # Operator discard: hides the lead from pipeline, board sync, pickers, and
    # run context, and blocks routines from re-creating it. Restorable.
    discarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    activities: Mapped[list["LeadActivity"]] = relationship(
        back_populates="lead",
        cascade="all, delete-orphan",
        order_by="LeadActivity.occurred_on",
    )


class LeadActivity(Base):
    __tablename__ = "lead_activity"
    __table_args__ = (
        # Idempotency across overlapping sync windows (spec §6): one activity
        # row per Gmail message, NULLs exempt.
        Index(
            "uq_lead_activity_gmail_msg_id",
            "gmail_msg_id",
            unique=True,
            postgresql_where=text("gmail_msg_id IS NOT NULL"),
            sqlite_where=text("gmail_msg_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    type: Mapped[LeadActivityType] = mapped_column(db_enum(LeadActivityType))
    occurred_on: Mapped[date] = mapped_column(Date)
    detail: Mapped[str | None] = mapped_column(Text)
    source: Mapped[ActivitySource] = mapped_column(
        db_enum(ActivitySource), default=ActivitySource.MANUAL
    )
    gmail_msg_id: Mapped[str | None] = mapped_column(String(128))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    lead: Mapped[Lead] = relationship(back_populates="activities")
