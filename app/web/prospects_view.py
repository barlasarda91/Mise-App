"""Prospects page: the lead discovery review queue.

Approve → a Pipeline lead at New (plus a first-outreach task, and the
first-touch draft linked to it so sending advances the lead). Reject → never
surfaced again. Mute → hidden for MUTE_DAYS, then fair game. Rejecting or
muting discards the prospect's unsent first-touch draft.
"""

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db import db_session
from app.models import (
    ActivitySource,
    DraftStatus,
    EmailDraft,
    Lead,
    LeadActivity,
    LeadActivityType,
    LeadStage,
    OPEN_LEAD_STAGES,
    Prospect,
    ProspectStatus,
)
from app.routines.discovery import MUTE_DAYS, REGION_LABELS, SEGMENT_LABELS, SEGMENT_MAILBOX, name_key
from app.settings import get_settings

RECENT_DECIDED = 20

_IG_HANDLE = re.compile(r"^@?([A-Za-z0-9_.]{1,30})$")


def _safe_url(url: str | None) -> str | None:
    """Model-written links render as hrefs: http(s) only."""
    return url if url and url.startswith(("http://", "https://")) else None


def _instagram_url(handle: str | None) -> str | None:
    match = _IG_HANDLE.match((handle or "").strip())
    return f"https://www.instagram.com/{match.group(1)}/" if match else None


def _today() -> date:
    return datetime.now(ZoneInfo(get_settings().default_tz)).date()


def _card(p: Prospect, draft: EmailDraft | None) -> dict:
    channel = "email" if p.contact_email else "DM + walk-in"
    return {
        "id": p.id,
        "name": p.business_name,
        "segment": SEGMENT_LABELS.get(p.segment, p.segment.value),
        "region": REGION_LABELS.get(p.region, p.region.value),
        "city": p.city,
        "address": p.address,
        "status": p.status.value,
        "muted_until": p.muted_until,
        "fit": p.fit_score,
        "why": p.why_fit,
        "angle": p.outreach_angle,
        "current_coffee": p.current_coffee,
        "website": _safe_url(p.website),
        "instagram": p.instagram,
        "instagram_url": _instagram_url(p.instagram),
        "phone": p.phone,
        "rating": p.rating,
        "review_count": p.review_count,
        "contact": ", ".join(x for x in (p.contact_name, p.contact_role) if x),
        "contact_email": p.contact_email,
        "channel": channel,
        "mailbox": SEGMENT_MAILBOX.get(p.segment),
        "dm_script": p.dm_script,
        "walk_in_note": p.walk_in_note,
        "sources": [u for u in (p.sources or []) if isinstance(u, str) and _safe_url(u)],
        "draft_id": draft.id if draft else None,
        "draft_state": (
            None if draft is None else "sent" if draft.sent_at else draft.status.value
        ),
        "lead_id": p.lead_id,
        "matched_lead_id": p.matched_lead_id,
        "found_on": p.created_at,
    }


def load_queue() -> dict:
    today = _today()
    try:
        with db_session() as s:
            rows = s.scalars(select(Prospect).order_by(Prospect.fit_score.desc(), Prospect.id.desc())).all()
            drafts = {
                d.id: d
                for d in s.scalars(
                    select(EmailDraft).where(EmailDraft.id.in_([p.draft_id for p in rows if p.draft_id]))
                )
            }
            review, muted, decided = [], [], []
            for p in rows:
                card = _card(p, drafts.get(p.draft_id))
                if p.status == ProspectStatus.NEW:
                    review.append(card)
                elif p.status == ProspectStatus.MUTED and p.muted_until and p.muted_until > today:
                    muted.append(card)
                else:
                    decided.append(card)
            decided.sort(key=lambda c: c["id"], reverse=True)
            return {"review": review, "muted": muted, "decided": decided[:RECENT_DECIDED]}
    except Exception:
        return {"review": [], "muted": [], "decided": []}


def review_count() -> int:
    """Prospects awaiting a decision — the nav badge."""
    try:
        with db_session() as s:
            return len(
                s.scalars(select(Prospect.id).where(Prospect.status == ProspectStatus.NEW)).all()
            )
    except Exception:
        return 0


def _discard_unsent_draft(s, prospect: Prospect) -> bool:
    if not prospect.draft_id:
        return False
    draft = s.get(EmailDraft, prospect.draft_id)
    if draft is None or draft.sent_at or draft.status == DraftStatus.DISCARDED:
        return False
    draft.status = DraftStatus.DISCARDED
    return True


def approve(prospect_id: int) -> str:
    from app.routines.tools import _create_task_impl

    today = _today()
    with db_session() as s:
        p = s.get(Prospect, prospect_id)
        if p is None:
            return "Prospect not found."
        if p.status == ProspectStatus.APPROVED and p.lead_id:
            return f"{p.business_name} is already in the Pipeline."

        # Reuse a lead that appeared meanwhile (manual entry, inbound inquiry).
        lead = None
        for candidate in s.scalars(
            select(Lead).where(Lead.stage.in_(OPEN_LEAD_STAGES), Lead.discarded_at.is_(None))
        ):
            if name_key(candidate.business_name) == p.name_key:
                lead = candidate
                break
        if lead is None:
            lead = Lead(
                business_name=p.business_name,
                contact_name=p.contact_name,
                contact_email=p.contact_email,
                contact_phone=p.phone,
                location=", ".join(x for x in (p.city, REGION_LABELS.get(p.region)) if x) or None,
                lead_source="discovery",
                stage=LeadStage.NEW,
                stage_since=today,
            )
            s.add(lead)
            s.flush()
            s.add(
                LeadActivity(
                    lead_id=lead.id,
                    type=LeadActivityType.NOTE,
                    occurred_on=today,
                    detail=f"From lead discovery ({SEGMENT_LABELS.get(p.segment)}, fit {p.fit_score}/5). "
                    f"{p.why_fit} Angle: {p.outreach_angle}",
                    source=ActivitySource.MANUAL,
                )
            )

        draft = s.get(EmailDraft, p.draft_id) if p.draft_id else None
        if draft is not None and draft.related_lead_id is None:
            # Linked, so sending it logs the email and moves the lead to Contacted.
            draft.related_lead_id = lead.id

        if p.contact_email and draft is not None and draft.status != DraftStatus.DISCARDED:
            # Approving is a decision about the LEAD, never the email: the
            # first-touch draft stays unsent in Drafts for its own review.
            title = f"Review first-touch draft to {p.business_name}"
            description = (
                f"Draft #{draft.id} is waiting UNSENT in Drafts — approving the "
                f"prospect did not approve the email. Review/edit it there and "
                f"send only if it's right. Angle: {p.outreach_angle}"
            )
        else:
            title = f"First outreach to {p.business_name} — DM / walk-in"
            parts = [f"Angle: {p.outreach_angle}"]
            if p.instagram:
                parts.append(f"Instagram: {p.instagram}")
            if p.phone:
                parts.append(f"Phone: {p.phone}")
            if p.dm_script:
                parts.append(f"DM:\n{p.dm_script}")
            if p.walk_in_note:
                parts.append(f"Walk-in:\n{p.walk_in_note}")
            description = "\n\n".join(parts)
        _create_task_impl(
            s,
            category="wholesale_leads",
            title=title,
            dedup_key=f"task:prospect:{p.id}",
            description=description,
            due_date=today.isoformat(),
            assignee=None,
            source_ref={"lead_id": lead.id},
        )

        p.status = ProspectStatus.APPROVED
        p.lead_id = lead.id
        p.muted_until = None
        p.decided_at = datetime.now(timezone.utc)
        return f"{p.business_name} → Pipeline (New)."


def reject(prospect_id: int) -> str:
    with db_session() as s:
        p = s.get(Prospect, prospect_id)
        if p is None:
            return "Prospect not found."
        discarded = _discard_unsent_draft(s, p)
        p.status = ProspectStatus.REJECTED
        p.muted_until = None
        p.decided_at = datetime.now(timezone.utc)
        return f"Rejected {p.business_name} — it won't come back." + (" Draft discarded." if discarded else "")


def mute(prospect_id: int) -> str:
    with db_session() as s:
        p = s.get(Prospect, prospect_id)
        if p is None:
            return "Prospect not found."
        discarded = _discard_unsent_draft(s, p)
        p.status = ProspectStatus.MUTED
        p.muted_until = _today() + timedelta(days=MUTE_DAYS)
        p.decided_at = datetime.now(timezone.utc)
        return f"Muted {p.business_name} until {p.muted_until}." + (" Draft discarded." if discarded else "")


def unmute(prospect_id: int) -> str:
    with db_session() as s:
        p = s.get(Prospect, prospect_id)
        if p is None:
            return "Prospect not found."
        p.status = ProspectStatus.NEW
        p.muted_until = None
        p.decided_at = None
        return f"{p.business_name} is back in the review queue."
