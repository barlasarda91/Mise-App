"""Lead discovery: prospects the discovery routine found, awaiting review.

A prospect is not a lead yet. Arda approves it (→ Pipeline lead at New),
rejects it (never surfaced again), or mutes it (hidden for 60 days).
"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PortableJSON, TimestampMixin, db_enum
from app.models.enums import ProspectRegion, ProspectSegment, ProspectStatus


class Prospect(TimestampMixin, Base):
    __tablename__ = "prospects"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_name: Mapped[str] = mapped_column(String(200))
    # Normalized name used for dedup across runs (see discovery.name_key).
    name_key: Mapped[str] = mapped_column(String(200), index=True)
    segment: Mapped[ProspectSegment] = mapped_column(db_enum(ProspectSegment))
    region: Mapped[ProspectRegion] = mapped_column(db_enum(ProspectRegion), index=True)
    status: Mapped[ProspectStatus] = mapped_column(
        db_enum(ProspectStatus), default=ProspectStatus.NEW, index=True
    )
    muted_until: Mapped[date | None] = mapped_column(Date)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    city: Mapped[str | None] = mapped_column(String(100))
    address: Mapped[str | None] = mapped_column(String(300))
    website: Mapped[str | None] = mapped_column(String(300))
    instagram: Mapped[str | None] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(50))
    google_place_id: Mapped[str | None] = mapped_column(String(200), index=True)
    rating: Mapped[float | None] = mapped_column(Numeric(2, 1))
    review_count: Mapped[int | None] = mapped_column(Integer)

    contact_name: Mapped[str | None] = mapped_column(String(200))
    contact_role: Mapped[str | None] = mapped_column(String(100))
    contact_email: Mapped[str | None] = mapped_column(String(320))

    fit_score: Mapped[int] = mapped_column(Integer)  # 1–5
    current_coffee: Mapped[str | None] = mapped_column(Text)
    why_fit: Mapped[str] = mapped_column(Text)
    outreach_angle: Mapped[str] = mapped_column(Text)
    dm_script: Mapped[str | None] = mapped_column(Text)
    walk_in_note: Mapped[str | None] = mapped_column(Text)
    sources: Mapped[list | None] = mapped_column(PortableJSON)
    # How the routine found it ("places: 24 hour diner Pasadena", "web: eater
    # best brunch riverside", "instagram: guest roaster post") — the per-channel
    # tuning signal for discovery analytics. None on rows from before 0018.
    discovered_via: Mapped[str | None] = mapped_column(String(200))

    draft_id: Mapped[int | None] = mapped_column(
        ForeignKey("email_drafts.id", ondelete="SET NULL")
    )
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"))
    # A closed (won/lost) Pipeline lead with the same name, for context.
    matched_lead_id: Mapped[int | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL")
    )
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
