"""Lead Discovery routine: finds cafes, restaurants and diners that should be
serving Boxx, researches them, and drafts the first touch.

Prospects land in a review queue (the Prospects page), never straight in the
Pipeline. Arda approves (→ lead at New), rejects (never again), or mutes
(hidden MUTE_DAYS). The routine only drafts — nothing here can send.

Importing this module registers the discovery tools and the routine's
server-side web tools into the engine toolkit.
"""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.engine.toolkit import ROUTINE_TOOLSETS, SERVER_TOOLS, ToolDef, get_run_context, register
from app.models import (
    Lead,
    OPEN_LEAD_STAGES,
    Prospect,
    ProspectRegion,
    ProspectSegment,
    ProspectStatus,
)
from app.settings import get_settings

ROUTINE_KEY = "lead_discovery"

MUTE_DAYS = 60
TARGET_PER_RUN = 3
MAX_PER_RUN = 5  # hard cap: a runaway run can't flood the review queue
PLACES_CALLS_PER_RUN = 15  # cost guard (~$0.035 per text search)

REGION_LABELS = {
    ProspectRegion.LOS_ANGELES: "Los Angeles",
    ProspectRegion.SAN_DIEGO: "San Diego",
    ProspectRegion.SAN_GABRIEL_VALLEY: "San Gabriel Valley",
    ProspectRegion.RIVERSIDE: "Riverside",
    ProspectRegion.SANTA_BARBARA: "Santa Barbara",
}

SEGMENT_LABELS = {
    ProspectSegment.MULTI_ROASTER: "Multi-roaster specialty",
    ProspectSegment.ALL_DAY_CAFE: "All-day volume cafe",
    ProspectSegment.VOLUME_DINER: "Volume diner",
    ProspectSegment.RESTAURANT: "Restaurant",
}

# Weekly focus rotation. Riverside and Santa Barbara are the expansion
# territories, so each gets two days in seven; the rest get one.
REGION_CYCLE = [
    ProspectRegion.LOS_ANGELES,
    ProspectRegion.RIVERSIDE,
    ProspectRegion.SANTA_BARBARA,
    ProspectRegion.SAN_DIEGO,
    ProspectRegion.RIVERSIDE,
    ProspectRegion.SAN_GABRIEL_VALLEY,
    ProspectRegion.SANTA_BARBARA,
]

# Specialty shops hear from Arda personally; volume accounts from the brand desk.
SEGMENT_MAILBOX = {
    ProspectSegment.MULTI_ROASTER: "arda",
    ProspectSegment.ALL_DAY_CAFE: "hello",
    ProspectSegment.VOLUME_DINER: "hello",
    ProspectSegment.RESTAURANT: "hello",
}

# Words that don't distinguish one business from another ("Play Coffee" and
# "Play Coffee Co." are the same prospect).
_NAME_NOISE = {
    "the", "a", "and", "co", "company", "coffee", "cafe", "café", "roasters",
    "roastery", "restaurant", "diner", "deli", "bar", "kitchen", "llc", "inc", "la",
}


def name_key(name: str) -> str:
    words = re.findall(r"[a-z0-9]+", (name or "").lower().replace("'", ""))
    kept = [w for w in words if w not in _NAME_NOISE]
    return " ".join(kept or words)


def _today() -> date:
    return datetime.now(ZoneInfo(get_settings().default_tz)).date()


def focus_region(day: date) -> ProspectRegion:
    return REGION_CYCLE[day.toordinal() % len(REGION_CYCLE)]


# ---------- tools ----------

_places_calls: dict = {}


def _search_places(session: Session, query: str, region=None, max_results: int = 10):
    from app.tools import places

    if not places.places_configured():
        return {
            "available": False,
            "note": "Google Places is not configured (GOOGLE_PLACES_API_KEY unset). "
            "Use web_search for addresses, phones and review volume instead, and "
            "say so in your report.",
        }
    run_id = get_run_context().get("run_id")
    used = _places_calls.get(run_id, 0)
    if used >= PLACES_CALLS_PER_RUN:
        return {
            "available": False,
            "note": f"Places budget for this run is spent ({PLACES_CALLS_PER_RUN} searches). "
            "Finish with web_search.",
        }
    _places_calls[run_id] = used + 1
    if region is not None:
        ProspectRegion(region)  # validate
    return {"places": places.search_text(query, region, max_results)}


def _known_match(session: Session, key: str, place_id: str | None) -> Prospect | None:
    clauses = [Prospect.name_key == key]
    if place_id:
        clauses.append(Prospect.google_place_id == place_id)
    from sqlalchemy import or_

    return session.scalar(select(Prospect).where(or_(*clauses)).order_by(Prospect.id.desc()))


def _lead_match(session: Session, key: str, email: str | None) -> Lead | None:
    for lead in session.scalars(select(Lead)).all():
        same_email = email and lead.contact_email and lead.contact_email.lower() == email.lower()
        if same_email or name_key(lead.business_name) == key:
            return lead
    return None


def _record_prospect(
    session: Session,
    business_name: str,
    segment: str,
    region: str,
    fit_score: int,
    why_fit: str,
    outreach_angle: str,
    sources: list,
    city=None,
    address=None,
    website=None,
    instagram=None,
    phone=None,
    google_place_id=None,
    rating=None,
    review_count=None,
    current_coffee=None,
    contact_name=None,
    contact_role=None,
    contact_email=None,
    discovered_via=None,
    email_subject=None,
    email_body=None,
    dm_script=None,
    walk_in_note=None,
):
    seg = ProspectSegment(segment)
    reg = ProspectRegion(region)
    if not 1 <= int(fit_score) <= 5:
        raise ValueError("fit_score must be 1–5")
    if not sources:
        raise ValueError("sources must list the URLs your research relied on")
    if contact_email and not (email_subject and email_body):
        raise ValueError("a contact_email needs email_subject and email_body (the first-touch draft)")
    if not contact_email and not (dm_script and walk_in_note):
        raise ValueError(
            "no contact_email found: provide BOTH dm_script (Instagram DM or call opener) "
            "and walk_in_note (when to stop by, who to ask for, the talking point)"
        )

    ctx = get_run_context()
    run_id = ctx.get("run_id")
    if run_id is not None:
        recorded = session.scalar(
            select(func.count()).select_from(Prospect).where(Prospect.run_id == run_id)
        )
        if recorded >= MAX_PER_RUN:
            return {
                "outcome": "run_cap_reached",
                "note": f"{MAX_PER_RUN} prospects already recorded this run — stop and write the report.",
            }

    key = name_key(business_name)
    today = _today()

    lead = _lead_match(session, key, contact_email)
    matched_closed = None
    if lead is not None:
        if lead.discarded_at is not None:
            return {
                "outcome": "skipped",
                "reason": "Arda discarded this business as a Pipeline lead — never re-surface it.",
            }
        if lead.stage in OPEN_LEAD_STAGES:
            return {
                "outcome": "skipped",
                "reason": f"already an open Pipeline lead (#{lead.id}, {lead.stage.value}).",
            }
        matched_closed = lead  # won/lost before: allowed, shown as context

    existing = _known_match(session, key, google_place_id)
    if existing is not None:
        if existing.status == ProspectStatus.REJECTED:
            return {"outcome": "skipped", "reason": "Arda rejected this prospect — never re-surface it."}
        if existing.status == ProspectStatus.NEW:
            return {"outcome": "skipped", "reason": f"already awaiting Arda's review (prospect #{existing.id})."}
        if existing.status == ProspectStatus.APPROVED:
            return {"outcome": "skipped", "reason": f"already approved into the Pipeline (prospect #{existing.id})."}
        if existing.muted_until and existing.muted_until > today:
            return {
                "outcome": "skipped",
                "reason": f"Arda muted this prospect until {existing.muted_until}.",
            }
        prospect = existing  # mute expired: resurface the same row
    else:
        prospect = Prospect(business_name=business_name, name_key=key)
        session.add(prospect)

    prospect.business_name = business_name
    prospect.name_key = key
    prospect.segment = seg
    prospect.region = reg
    prospect.status = ProspectStatus.NEW
    prospect.muted_until = None
    prospect.decided_at = None
    prospect.city = city
    prospect.address = address
    prospect.website = website
    prospect.instagram = instagram
    prospect.phone = phone
    prospect.google_place_id = google_place_id
    prospect.rating = rating
    prospect.review_count = review_count
    prospect.current_coffee = current_coffee
    prospect.contact_name = contact_name
    prospect.contact_role = contact_role
    prospect.contact_email = contact_email
    prospect.fit_score = int(fit_score)
    prospect.why_fit = why_fit
    prospect.outreach_angle = outreach_angle
    prospect.dm_script = dm_script
    prospect.walk_in_note = walk_in_note
    prospect.sources = list(sources)[:12]
    prospect.discovered_via = (discovered_via or "").strip()[:200] or None
    prospect.matched_lead_id = matched_closed.id if matched_closed else None
    prospect.lead_id = None
    prospect.draft_id = None
    prospect.run_id = run_id
    session.flush()

    result = {"outcome": "recorded", "prospect_id": prospect.id}
    if contact_email:
        from app.routines.tools import _create_email_draft

        mailbox = SEGMENT_MAILBOX[seg]
        draft = _create_email_draft(
            session,
            mailbox=mailbox,
            to=[contact_email],
            subject=email_subject,
            body=email_body,
            purpose=f"prospect{prospect.id}",
        )
        prospect.draft_id = draft.get("draft_id")
        result["draft"] = {"draft_id": prospect.draft_id, "mailbox": mailbox}
    if matched_closed is not None:
        result["note"] = (
            f"matches a closed Pipeline lead (#{matched_closed.id}, {matched_closed.stage.value}) — "
            "make sure why_fit says why it's worth another conversation."
        )
    return result


register(
    ToolDef(
        name="search_places",
        description=(
            "Google Places text search for structured business data: address, phone, "
            "website, Maps link, rating, review count (a proxy for volume), opening hours, "
            "business status. Use queries like 'specialty coffee shop Riverside CA' or "
            "'24 hour diner Pasadena'. region biases results to that territory. Returns "
            "available=false when Places isn't configured — fall back to web_search."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "region": {
                    "anyOf": [
                        {"type": "string", "enum": [r.value for r in ProspectRegion]},
                        {"type": "null"},
                    ]
                },
                "max_results": {"type": "integer", "description": "1–20, default 10"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=_search_places,
        routines=(ROUTINE_KEY,),
    )
)

register(
    ToolDef(
        name="record_prospect",
        description=(
            "Record one researched prospect into Arda's review queue. Dedupes against "
            "past prospects (rejected = never, muted = until the mute ends) and open "
            "Pipeline leads, and answers outcome=skipped with the reason when it's "
            "known — then move on to another business. With contact_email, also writes "
            "the first-touch email draft (from arda@ for multi_roaster, hello@ for the "
            "volume segments — write the body in that mailbox's voice, signed Arda). "
            "Without an email, dm_script AND walk_in_note are both required."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "business_name": {"type": "string"},
                "segment": {"type": "string", "enum": [s.value for s in ProspectSegment]},
                "region": {"type": "string", "enum": [r.value for r in ProspectRegion]},
                "fit_score": {
                    "type": "integer",
                    "description": "1–5: 5 = obvious fit and reachable now; 3 = plausible; below 3 don't record.",
                },
                "why_fit": {
                    "type": "string",
                    "description": "2–4 sentences of evidence: what they serve now, volume signals, why Boxx fits.",
                },
                "outreach_angle": {
                    "type": "string",
                    "description": "The one specific hook for the first conversation.",
                },
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "URLs your findings came from (site, menu, IG, press, Maps).",
                },
                "discovered_via": {
                    "type": "string",
                    "description": (
                        "How you FIRST surfaced this business, as 'channel: detail' — e.g. "
                        "'places: 24 hour diner Pasadena', 'web: eater best brunch riverside', "
                        "'instagram: guest roaster announcement', 'press: LA Times new openings'. "
                        "Arda tunes the discovery strategy on these, so name the actual query/angle."
                    ),
                },
                "city": {"type": "string"},
                "address": {"type": "string"},
                "website": {"type": "string"},
                "instagram": {"type": "string", "description": "handle, e.g. @playcoffee"},
                "phone": {"type": "string"},
                "google_place_id": {"type": "string"},
                "rating": {"type": "number"},
                "review_count": {"type": "integer"},
                "current_coffee": {
                    "type": "string",
                    "description": "Who/what they serve now (roaster names, rotating guests, commodity) — or 'unknown'.",
                },
                "contact_name": {"type": "string"},
                "contact_role": {"type": "string", "description": "owner, coffee buyer, GM, …"},
                "contact_email": {"type": "string"},
                "email_subject": {"type": "string"},
                "email_body": {"type": "string"},
                "dm_script": {
                    "type": "string",
                    "description": "Short Instagram DM (or phone opener) Arda can send by hand.",
                },
                "walk_in_note": {
                    "type": "string",
                    "description": "Best day/time to stop by, who to ask for, and the talking point.",
                },
            },
            "required": [
                "business_name", "segment", "region", "fit_score", "why_fit",
                "outreach_angle", "sources", "discovered_via",
            ],
            "additionalProperties": False,
        },
        handler=_record_prospect,
        routines=(ROUTINE_KEY,),
    )
)

ROUTINE_TOOLSETS[ROUTINE_KEY] = ["list_open_leads", "record_prospect", "search_places"]

SERVER_TOOLS[ROUTINE_KEY] = [
    {
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": 30,
        "user_location": {
            "type": "approximate",
            "city": "Los Angeles",
            "region": "California",
            "country": "US",
            "timezone": "America/Los_Angeles",
        },
    },
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 20},
]


# ---------- runtime context ----------

MAX_CONTEXT_NAMES = 150


def context_lines(session: Session, today: date) -> list[str]:
    """Discovery's state snapshot: today's territory focus, the quota, and
    everything that must not be surfaced again."""
    from app.tools.places import places_configured

    focus = focus_region(today)
    lines = [
        "\n## Today's assignment",
        f"Focus territory: **{REGION_LABELS[focus]}** (`{focus.value}`). Spend most of the run "
        "here; a standout find elsewhere in the territory is fine.",
        f"Target: {TARGET_PER_RUN} well-researched prospects (hard cap {MAX_PER_RUN}). "
        "Fewer strong ones beat padding the queue.",
        "Google Places: "
        + ("configured — use search_places for structured data."
           if places_configured()
           else "NOT configured — work from web_search/web_fetch alone and say so in the report."),
    ]

    since = datetime.now(ZoneInfo("UTC")) - timedelta(days=14)
    recent = session.execute(
        select(Prospect.segment, func.count())
        .where(Prospect.created_at >= since)
        .group_by(Prospect.segment)
    ).all()
    counts = {seg: n for seg, n in recent}
    lines.append(
        "Segment mix, last 14 days: "
        + ", ".join(f"{SEGMENT_LABELS[s]} {counts.get(s, 0)}" for s in ProspectSegment)
        + " — lean toward whichever is thin."
    )

    prospects = session.scalars(select(Prospect).order_by(Prospect.id.desc())).all()
    groups: dict[str, list[str]] = {"review": [], "approved": [], "rejected": [], "muted": []}
    for p in prospects:
        if p.status == ProspectStatus.NEW:
            groups["review"].append(p.business_name)
        elif p.status == ProspectStatus.APPROVED:
            groups["approved"].append(p.business_name)
        elif p.status == ProspectStatus.REJECTED:
            groups["rejected"].append(p.business_name)
        elif p.muted_until and p.muted_until > today:
            groups["muted"].append(f"{p.business_name} (until {p.muted_until})")

    lines.append("\n## Do not research these again (record_prospect would skip them anyway)")
    labels = {
        "review": "Awaiting Arda's review",
        "approved": "Approved into the Pipeline",
        "rejected": "Rejected by Arda — never",
        "muted": "Muted by Arda",
    }
    any_listed = False
    for key, label in labels.items():
        names = groups[key][:MAX_CONTEXT_NAMES]
        if names:
            any_listed = True
            lines.append(f"- {label}: " + "; ".join(names))

    leads = session.scalars(
        select(Lead).where(
            Lead.stage.in_(OPEN_LEAD_STAGES) | Lead.discarded_at.isnot(None)
        )
    ).all()
    open_names = [l.business_name for l in leads if l.discarded_at is None]
    discarded = [l.business_name for l in leads if l.discarded_at is not None]
    if open_names:
        any_listed = True
        lines.append("- Open Pipeline leads: " + "; ".join(open_names[:MAX_CONTEXT_NAMES]))
    if discarded:
        any_listed = True
        lines.append("- Discarded by Arda: " + "; ".join(discarded[:MAX_CONTEXT_NAMES]))
    if not any_listed:
        lines.append("- (nothing yet — this is an early run)")
    return lines
