"""Skip-if-quiet pre-flight (cost roadmap phase 1a).

Before a scheduled run calls the model, plain code checks whether anything
actually happened since the last gather: new mail in the routine's mailboxes,
and (for the agenda) a changed calendar. All quiet -> the run is recorded as
`skipped` and the model is never called. Fail open: any error here means run.

Always runs regardless: manual "Run now" triggers, the first run of a day
(morning briefing / A-R check / cadence flips at midnight), and cold starts
with no gather state yet.
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models import AppState, Routine, Run, RunStatus, RunTrigger, SyncState
from app.models.enums import FromMailbox
from app.settings import get_settings

log = logging.getLogger(__name__)

CAL_HASH_KEY = "preflight:calendar_hash"

GMAIL_SOURCES = {"gmail_arda": FromMailbox.ARDA, "gmail_hello": FromMailbox.HELLO}


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _is_first_run_of_day(session, routine: Routine) -> bool:
    tz = ZoneInfo(get_settings().default_tz)
    latest = session.scalars(
        select(Run)
        .where(Run.routine_id == routine.id, Run.status == RunStatus.COMPLETED)
        .order_by(Run.started_at.desc(), Run.id.desc())
        .limit(1)
    ).first()
    if latest is None or latest.started_at is None:
        return True
    return _aware(latest.started_at).astimezone(tz).date() < datetime.now(tz).date()


def _new_mail_count(mailbox: FromMailbox, since: datetime) -> int:
    from app.tools import gmail

    epoch = int(_aware(since).timestamp())
    return sum(
        gmail.count_messages(mailbox, f"{scope} after:{epoch}")
        for scope in ("in:inbox", "in:sent")
    )


def _calendar_fingerprint() -> str | None:
    """Stable hash of the next few days of events; None when unavailable."""
    try:
        from app.tools.calendar import list_events

        tz = ZoneInfo(get_settings().default_tz)
        now = datetime.now(tz)
        events = list_events(now - timedelta(days=1), now + timedelta(days=3))
        key = sorted(
            [
                str(e.get("id")),
                str(e.get("updated")),
                json.dumps(e.get("start"), sort_keys=True, default=str),
                json.dumps(e.get("end"), sort_keys=True, default=str),
                str(e.get("summary")),
            ]
            for e in events
        )
        return hashlib.sha256(json.dumps(key).encode()).hexdigest()
    except Exception as exc:
        log.warning("preflight calendar fingerprint unavailable: %s", exc)
        return None


def _store_calendar_fp(session, fp: str) -> None:
    row = session.get(AppState, CAL_HASH_KEY)
    if row is None:
        session.add(AppState(key=CAL_HASH_KEY, value={"hash": fp}))
    elif row.value.get("hash") != fp:
        row.value = {"hash": fp}


def skip_reason(session, routine: Routine, trigger: RunTrigger) -> str | None:
    """None -> execute the run. A string -> record a skipped run with it."""
    try:
        if trigger != RunTrigger.SCHEDULED:
            return None  # "Run now" is an explicit ask

        if _is_first_run_of_day(session, routine):
            # The morning run always executes; refresh the calendar baseline
            # so intraday diffs compare against today, not yesterday.
            if routine.key == "daily_agenda":
                fp = _calendar_fingerprint()
                if fp is not None:
                    _store_calendar_fp(session, fp)
            return None

        gather = {
            row.source.value: row.last_run_at
            for row in session.scalars(
                select(SyncState).where(SyncState.routine_id == routine.id)
            )
        }

        quiet_notes = []
        for source, mailbox in GMAIL_SOURCES.items():
            if source not in (routine.connectors or []):
                continue
            since = gather.get(source)
            if since is None:
                return None  # cold start — let the run establish state
            if _new_mail_count(mailbox, since) > 0:
                return None
            quiet_notes.append(f"{mailbox.value}@ since {_aware(since).astimezone(ZoneInfo(get_settings().default_tz)).strftime('%H:%M')}")
        if not quiet_notes:
            return None  # no gmail sources to judge quiet by

        if routine.key == "daily_agenda":
            fp = _calendar_fingerprint()
            if fp is None:
                return None  # can't verify the calendar — run
            row = session.get(AppState, CAL_HASH_KEY)
            stored = (row.value or {}).get("hash") if row else None
            _store_calendar_fp(session, fp)
            if stored != fp:
                return None  # calendar changed
            quiet_notes.append("calendar unchanged")

        return "no new mail (" + "; ".join(quiet_notes) + ")"
    except Exception as exc:
        log.warning("preflight check errored — running anyway: %s", exc)
        return None
