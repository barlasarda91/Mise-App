"""Prospects page: the lead discovery review queue.

Approve → a Pipeline lead at New plus a first-outreach task (Arda does the
outreach himself — the routine never drafts email). Reject → never surfaced
again. Mute → hidden for MUTE_DAYS, then fair game. Review cards carry
two separate tags — county (groups the queue) and reach-out method — each
derived automatically and overridable by Arda.
Legacy prospects from before the no-draft change may still carry a draft;
rejecting or muting discards it.
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

# Discovery territory -> county default (San Gabriel Valley is LA County).
REGION_COUNTY = {
    "los_angeles": "LA County",
    "san_gabriel_valley": "LA County",
    "riverside": "Riverside County",
    "san_diego": "San Diego County",
    "santa_barbara": "Santa Barbara County",
}
REACH_METHODS = ["Email", "DM", "Walk-in", "Phone"]


def _effective_county(p: Prospect) -> str:
    return p.county or REGION_COUNTY.get(p.region.value, "Other")


def _effective_reach(p: Prospect) -> str:
    if p.reach_method:
        return p.reach_method
    return "Email" if p.contact_email else "DM"


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
        "bucket": p.bucket,
        "county": _effective_county(p),
        "reach": _effective_reach(p),
        "lat": float(p.lat) if p.lat is not None else None,
        "lng": float(p.lng) if p.lng is not None else None,
        "visit_on": p.visit_on,
        "last_visited_on": p.last_visited_on,
        "needs_research": p.needs_research,
        "notes": p.notes,
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
            # Today's visits: every prospect with a planned visit, whatever
            # its review status (an approved lead can still be walked into).
            # A date that slipped past stays on the list, flagged carried-over.
            visits = []
            for p in rows:
                if p.visit_on is None:
                    continue
                card = _card(p, drafts.get(p.draft_id))
                card["carried_over"] = p.visit_on < today
                visits.append(card)
            visits.sort(key=lambda c: (c["visit_on"], c["county"], c["name"]))
            # Arda's questions, oldest first, attached to review + visit cards.
            from app.models import ProspectQuestion

            qmap: dict[int, list[dict]] = {}
            for q in s.scalars(select(ProspectQuestion).order_by(ProspectQuestion.id)):
                qmap.setdefault(q.prospect_id, []).append(
                    {"id": q.id, "q": q.question, "answer": q.answer, "pending": q.answer is None}
                )
            for card in review + visits:
                card["qa"] = qmap.get(card["id"], [])
            counties = sorted({c["county"] for c in review})
            groups = [
                {"name": name, "cards": [c for c in review if c["county"] == name]}
                for name in counties
            ]
            reaches = REACH_METHODS + sorted(
                {c["reach"] for c in review} - set(REACH_METHODS)
            )
            try:
                from app.tools.places import places_configured

                places_on = places_configured()
            except Exception:
                places_on = False
            return {
                "review": review,
                "groups": groups,
                "counties": counties,
                "reaches": reaches,
                "muted": muted,
                "decided": decided[:RECENT_DECIDED],
                "visits": visits,
                "places_on": places_on,
            }
    except Exception:
        return {"review": [], "groups": [], "counties": [], "reaches": REACH_METHODS,
                "muted": [], "decided": [], "visits": [], "places_on": False}


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

        # Legacy drafts (from before the no-auto-draft change) still link so
        # sending one logs the email and advances the lead.
        draft = s.get(EmailDraft, p.draft_id) if p.draft_id else None
        if draft is not None and draft.related_lead_id is None:
            draft.related_lead_id = lead.id

        title = f"First outreach to {p.business_name}"
        parts = [f"Angle: {p.outreach_angle}"]
        if p.contact_email:
            parts.append(f"Email: {p.contact_email}"
                         + (f" ({p.contact_name})" if p.contact_name else ""))
        if p.instagram:
            parts.append(f"Instagram: {p.instagram}")
        if p.phone:
            parts.append(f"Phone: {p.phone}")
        if p.dm_script:
            parts.append(f"DM:\n{p.dm_script}")
        if p.walk_in_note:
            parts.append(f"Walk-in:\n{p.walk_in_note}")
        if p.notes:
            parts.append(f"Your notes:\n{p.notes}")
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

        # Visit notes jotted before approval ride along as one activity;
        # notes added after approval mirror themselves live (see add_note).
        if p.notes:
            s.add(
                LeadActivity(
                    lead_id=lead.id,
                    type=LeadActivityType.NOTE,
                    occurred_on=today,
                    detail=f"Arda's visit notes:\n{p.notes}",
                    source=ActivitySource.MANUAL,
                )
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


GEOCODE_STATE_KEY = "prospects:geocoded_v1"
GEOCODE_CAP = 40


def locate_prospects() -> tuple[int, int, str | None]:
    """Geocode review-queue prospects that have no pin yet (capped per
    call). Returns (located, still_missing, error). The FIRST lookup is
    probed un-swallowed: when the Places API itself is broken (key invalid,
    API not enabled, billing off) every row would fail identically, so that
    one error surfaces to the page instead of reading as 'nothing found'.
    Safe to repeat — only lat-less rows cost a lookup."""
    from app.tools import places

    if not places.places_configured():
        return 0, -1, None  # unconfigured: the page explains
    with db_session() as s:
        rows = s.scalars(
            select(Prospect).where(
                Prospect.lat.is_(None), Prospect.status == ProspectStatus.NEW
            ).order_by(Prospect.id.desc())
        ).all()
        located = 0
        for i, p in enumerate(rows[:GEOCODE_CAP]):
            if i == 0:
                try:
                    query = ", ".join(x for x in (p.business_name, p.address or p.city) if x)
                    results = places.search_text(query, p.region.value, max_results=1)
                except Exception as exc:
                    return 0, len(rows), f"{exc}"[:300]
                hit = results[0] if results and results[0].get("lat") is not None else None
            else:
                hit = places.geocode_place(p.business_name, p.address or p.city, p.region.value)
            if hit:
                p.lat, p.lng = hit["lat"], hit["lng"]
                located += 1
        return located, len(rows) - located, None


def geocode_missing_prospects() -> int:
    """Startup pass: one-time (AppState-gated) run of locate_prospects, so
    a deploy pins the existing queue without anyone pressing the button."""
    from app.models import AppState
    from app.tools.places import places_configured

    if not places_configured():
        return 0
    with db_session() as s:
        if s.get(AppState, GEOCODE_STATE_KEY) is not None:
            return 0
        s.add(AppState(key=GEOCODE_STATE_KEY, value={"ran": True}))
    located, _, _ = locate_prospects()
    return located


def set_tag(prospect_id: int, field: str, value: str) -> str:
    """Set one of the two tag dimensions ('' returns to the derived
    default). county groups the queue; reach_method shows as a pill."""
    if field not in ("county", "reach_method"):
        return "Unknown tag."
    value = " ".join((value or "").split())[:60 if field == "county" else 30]
    with db_session() as s:
        p = s.get(Prospect, prospect_id)
        if p is None:
            return "Prospect not found."
        setattr(p, field, value or None)
        label = "county" if field == "county" else "reach-out"
        if value:
            return f"{p.business_name}: {label} → {value}."
        return f"{p.business_name}: {label} back to automatic."


def set_bucket(prospect_id: int, bucket: str) -> str:
    """Move a prospect into one of Arda's own categories ('' clears it)."""
    bucket = " ".join((bucket or "").split())[:60]
    with db_session() as s:
        p = s.get(Prospect, prospect_id)
        if p is None:
            return "Prospect not found."
        p.bucket = bucket or None
        if bucket:
            return f"{p.business_name} → {bucket}."
        return f"{p.business_name} is uncategorized again."


def unmute(prospect_id: int) -> str:
    with db_session() as s:
        p = s.get(Prospect, prospect_id)
        if p is None:
            return "Prospect not found."
        p.status = ProspectStatus.NEW
        p.muted_until = None
        p.decided_at = None
        return f"{p.business_name} is back in the review queue."


def set_visit(prospect_id: int, action: str) -> str:
    """Today's-visits lifecycle. plan → on today's list; done → off the list
    with the visit date remembered on the card; skip → off the list quietly."""
    if action not in ("plan", "done", "skip"):
        return "Unknown visit action."
    today = _today()
    with db_session() as s:
        p = s.get(Prospect, prospect_id)
        if p is None:
            return "Prospect not found."
        if action == "plan":
            if p.visit_on is not None and p.visit_on <= today:
                return f"{p.business_name} is already on today's visits."
            p.visit_on = today
            return f"{p.business_name} → today's visits."
        if action == "done":
            p.visit_on = None
            p.last_visited_on = today
            return f"{p.business_name}: visited ✓ — decide the card whenever you're ready."
        p.visit_on = None
        return f"{p.business_name} came off the visit list."


def add_prospect(name: str, visit_today: bool = False) -> str:
    """Manual add from the little box on the Prospects page. Dedup by the
    same name key discovery uses; Google Places (when configured) fills in
    address, phone, website and the map pin right away, and the stub is
    flagged needs_research so the Lead Discovery run gives it the same full
    card every found prospect gets. Typing a previously rejected or muted
    shop deliberately re-opens it."""
    from app.models import ProspectRegion, ProspectSegment
    from app.routines.discovery import _lead_match

    name = " ".join((name or "").split())[:200]
    if not name:
        return "Type a shop name first."
    key = name_key(name)
    today = _today()
    with db_session() as s:
        existing = s.scalars(select(Prospect).where(Prospect.name_key == key)).first()
        if existing is not None:
            note = ""
            if existing.status != ProspectStatus.NEW:
                was = existing.status.value
                existing.status = ProspectStatus.NEW
                existing.muted_until = None
                existing.decided_at = None
                note = f" (was {was} — back in the queue)"
            if visit_today:
                existing.visit_on = today
                note += " · on today's visits"
            return f"{existing.business_name} is already in Prospects{note}."

        # Already a live Pipeline lead? Point there instead of a stub the
        # research pass could never fill (record_prospect skips open leads).
        lead = _lead_match(s, key, None)
        if lead is not None and lead.discarded_at is None and lead.stage in OPEN_LEAD_STAGES:
            return f"{lead.business_name} is already an open Pipeline lead (#{lead.id})."

        place = None
        try:
            from app.tools.places import places_configured, search_text

            if places_configured():
                hits = search_text(f"{name}, Los Angeles area", max_results=1)
                place = hits[0] if hits else None
        except Exception:
            place = None  # a bare-name prospect still gets added

        p = Prospect(
            business_name=(place or {}).get("name") or name,
            name_key=key,
            segment=ProspectSegment.ALL_DAY_CAFE,
            region=ProspectRegion.LOS_ANGELES,
            fit_score=3,
            why_fit="Added by hand — research queued for the next Lead Discovery pass.",
            outreach_angle="Arda's own find — angle is his call.",
            discovered_via=f"manual: {name}"[:200],
            visit_on=today if visit_today else None,
            needs_research=True,
        )
        if place:
            p.address = (place.get("address") or "")[:300] or None
            p.phone = (place.get("phone") or "")[:50] or None
            p.website = (place.get("website") or "")[:300] or None
            p.google_place_id = place.get("place_id")
            p.rating = place.get("rating")
            p.review_count = place.get("review_count")
            p.lat = place.get("lat")
            p.lng = place.get("lng")
        s.add(p)
        s.flush()
        added_name = p.business_name
        looked_up = " — details from Google Places" if place else ""
        extra = " · on today's visits" if visit_today else ""
    research = (
        " Research starts in a couple of minutes."
        if _queue_research()
        else " The next Lead Discovery run researches it."
    )
    return f"Added {added_name} to the review queue{looked_up}{extra}.{research}"


def _queue_research() -> bool:
    """Nudge a debounced Lead Discovery run so hand-added stubs get their
    research without waiting for tomorrow 7:30. False when the scheduler
    isn't running (dev/tests) — the daily run covers it then."""
    try:
        from app.models import Routine
        from app.scheduler import trigger_run_soon

        with db_session() as s:
            routine = s.scalar(select(Routine).where(Routine.key == "lead_discovery"))
            routine_id = routine.id if routine is not None and routine.enabled else None
        if routine_id is None:
            return False
        return trigger_run_soon(routine_id)
    except Exception:
        return False


MAX_NOTES_CHARS = 8000


def add_note(prospect_id: int, text: str) -> str:
    """Append a timestamped note to the prospect (visits tab composer). It
    shows on the review card, rides into the Pipeline on approval, and when
    the prospect is already a lead it mirrors straight onto the lead's
    activity timeline as a visit touchpoint."""
    text = (text or "").strip()
    if not text:
        return "Write the note first."
    text = text[:2000]
    today = _today()
    stamp = today.strftime("%b %d")
    with db_session() as s:
        p = s.get(Prospect, prospect_id)
        if p is None:
            return "Prospect not found."
        line = f"[{stamp}] {text}"
        p.notes = (f"{p.notes}\n{line}" if p.notes else line)[-MAX_NOTES_CHARS:]
        mirrored = ""
        if p.lead_id:
            s.add(
                LeadActivity(
                    lead_id=p.lead_id,
                    type=LeadActivityType.VISIT,
                    occurred_on=today,
                    detail=text,
                    source=ActivitySource.MANUAL,
                )
            )
            mirrored = " — logged on the Pipeline lead too"
        return f"Note saved on {p.business_name}{mirrored}."


MAX_QUESTION_CHARS = 300


def ask_question(prospect_id: int, text: str) -> str:
    """Ask-Claude on the visits tab: store the question and fire the
    on-demand Prospect Q&A run (debounced — rapid questions share a run)."""
    text = " ".join((text or "").split())[:MAX_QUESTION_CHARS]
    if not text:
        return "Type the question first."
    from app.models import ProspectQuestion

    with db_session() as s:
        p = s.get(Prospect, prospect_id)
        if p is None:
            return "Prospect not found."
        s.add(ProspectQuestion(
            prospect_id=p.id, question=text, asked_at=datetime.now(timezone.utc)
        ))
        name = p.business_name
    if _queue_question_run():
        return f"Asked about {name} — Claude is digging; the answer lands on the card in a few minutes."
    return (f"Question on {name} saved, but the Prospect Q&A routine is disabled — "
            "enable it in Settings to get answers.")


def _queue_question_run() -> bool:
    try:
        from app.models import Routine
        from app.scheduler import trigger_run_soon

        with db_session() as s:
            routine = s.scalar(select(Routine).where(Routine.key == "prospect_question"))
            routine_id = routine.id if routine is not None and routine.enabled else None
        if routine_id is None:
            return False
        return trigger_run_soon(routine_id, delay_seconds=15)
    except Exception:
        return False
