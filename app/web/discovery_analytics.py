"""Discovery analytics: the weekly tuning view over the prospect log.

Prospect rows are never deleted — approved, rejected and muted all persist —
so this aggregates the full history: how many the routine finds per week,
which channels/queries/territories/segments produce prospects Arda actually
approves, and whether the model's fit scores track his decisions.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db import db_session
from app.models import Prospect, ProspectStatus
from app.settings import get_settings

WEEKS = 8
LOG_CAP = 500


def _la_date(dt: datetime):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(get_settings().default_tz)).date()


def channel(via: str | None) -> str:
    """'places: 24 hour diner Pasadena' → 'places'. Pre-0018 rows → 'unknown'."""
    head = (via or "").split(":", 1)[0].strip().lower()
    return head[:30] if head else "unknown"


def _bucket() -> dict:
    return {"found": 0, "approved": 0, "rejected": 0, "muted": 0, "pending": 0,
            "fit_sum": 0, "fit_n": 0}


def _tally(bucket: dict, p: Prospect) -> None:
    bucket["found"] += 1
    bucket["fit_sum"] += p.fit_score or 0
    bucket["fit_n"] += 1
    if p.status == ProspectStatus.APPROVED:
        bucket["approved"] += 1
    elif p.status == ProspectStatus.REJECTED:
        bucket["rejected"] += 1
    elif p.status == ProspectStatus.MUTED:
        bucket["muted"] += 1
    else:
        bucket["pending"] += 1


def _finish(bucket: dict) -> dict:
    decided = bucket["approved"] + bucket["rejected"] + bucket["muted"]
    bucket["approval_pct"] = round(100 * bucket["approved"] / decided) if decided else None
    bucket["avg_fit"] = round(bucket["fit_sum"] / bucket["fit_n"], 1) if bucket["fit_n"] else None
    bucket.pop("fit_sum"), bucket.pop("fit_n")
    return bucket


def discovery_analytics() -> dict:
    """Weekly counts, per-channel / per-region / per-segment outcomes, fit
    calibration, and the complete log — everything the weekly tuning
    conversation needs. Fail-soft empty shape when the table is missing."""
    empty = {"weeks": [], "channels": [], "queries": [], "regions": [],
             "segments": [], "calibration": None, "log": [], "total": 0}
    try:
        with db_session() as s:
            rows = s.scalars(select(Prospect).order_by(Prospect.id.desc())).all()
    except Exception:
        return empty
    if not rows:
        return empty

    today = datetime.now(ZoneInfo(get_settings().default_tz)).date()
    this_monday = today - timedelta(days=today.weekday())
    week_starts = [this_monday - timedelta(weeks=i) for i in range(WEEKS)]
    weeks = {ws: _bucket() for ws in week_starts}
    channels: dict[str, dict] = {}
    queries: dict[str, dict] = {}
    regions: dict[str, dict] = {}
    segments: dict[str, dict] = {}
    fit_by_decision: dict[str, list] = {"approved": [], "rejected": []}

    for p in rows:
        found = _la_date(p.created_at) if p.created_at else today
        ws = found - timedelta(days=found.weekday())
        if ws in weeks:
            _tally(weeks[ws], p)
        _tally(channels.setdefault(channel(p.discovered_via), _bucket()), p)
        if p.discovered_via:
            _tally(queries.setdefault(p.discovered_via, _bucket()), p)
        _tally(regions.setdefault(p.region.value, _bucket()), p)
        _tally(segments.setdefault(p.segment.value, _bucket()), p)
        if p.status == ProspectStatus.APPROVED:
            fit_by_decision["approved"].append(p.fit_score or 0)
        elif p.status == ProspectStatus.REJECTED:
            fit_by_decision["rejected"].append(p.fit_score or 0)

    def table(d: dict, sort_found=True) -> list[dict]:
        out = [{"key": k, **_finish(v)} for k, v in d.items()]
        if sort_found:
            out.sort(key=lambda r: r["found"], reverse=True)
        return out

    calibration = None
    if fit_by_decision["approved"] or fit_by_decision["rejected"]:
        calibration = {
            side: round(sum(vals) / len(vals), 1) if vals else None
            for side, vals in fit_by_decision.items()
        }

    log = [
        {
            "id": p.id,
            "name": p.business_name,
            "segment": p.segment.value,
            "region": p.region.value,
            "city": p.city,
            "fit": p.fit_score,
            "via": p.discovered_via or "—",
            "status": p.status.value,
            "found": _la_date(p.created_at) if p.created_at else None,
            "decided": _la_date(p.decided_at) if p.decided_at else None,
            "lead_id": p.lead_id,
        }
        for p in rows[:LOG_CAP]
    ]
    return {
        "weeks": [{"start": ws, **_finish(weeks[ws])} for ws in week_starts],
        "channels": table(channels),
        "queries": table(queries)[:15],
        "regions": table(regions),
        "segments": table(segments),
        "calibration": calibration,
        "log": log,
        "total": len(rows),
    }
