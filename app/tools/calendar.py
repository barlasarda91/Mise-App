"""Google Calendar tool layer: read events, create popup reminder events.

Reminders go on arda's calendar at 9 AM on the requested date, falling back to
10 or 11 AM if 9 is taken that day (spec §7.1). Dedup against re-runs is the
caller's job via the external_mutations ledger.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.settings import get_settings
from app.tools.google_client import calendar_service

REMINDER_HOURS = (9, 10, 11)
REMINDER_LENGTH_MIN = 30


def _calendar_address() -> str:
    settings = get_settings()
    return settings.calendar_address or settings.gmail_arda_address


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().default_tz)


# ---------- pure helpers (unit-tested without credentials) ----------


def pick_reminder_hour(busy_hours: set[int]) -> int:
    """First free slot of 9/10/11 AM; if all are taken, overlap at 9."""
    for hour in REMINDER_HOURS:
        if hour not in busy_hours:
            return hour
    return REMINDER_HOURS[0]


def busy_hours_from_events(events: list[dict], day: date, tz: ZoneInfo) -> set[int]:
    """Hours of `day` (local) covered by timed events. All-day events don't block."""
    busy: set[int] = set()
    for event in events:
        start_raw = (event.get("start") or {}).get("dateTime")
        end_raw = (event.get("end") or {}).get("dateTime")
        if not start_raw or not end_raw:
            continue  # all-day event
        start = datetime.fromisoformat(start_raw).astimezone(tz)
        end = datetime.fromisoformat(end_raw).astimezone(tz)
        cursor = start.replace(minute=0, second=0, microsecond=0)
        while cursor < end:
            if cursor.date() == day:
                busy.add(cursor.hour)
            cursor += timedelta(hours=1)
    return busy


# ---------- API operations ----------


def list_events(start: datetime, end: datetime) -> list[dict]:
    svc = calendar_service(_calendar_address())
    result = (
        svc.events()
        .list(
            calendarId="primary",
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    return result.get("items", [])


def events_on(day: date) -> list[dict]:
    tz = _tz()
    start = datetime.combine(day, time.min, tzinfo=tz)
    return list_events(start, start + timedelta(days=1))


def create_reminder(requested_date: date, summary: str, description: str) -> dict:
    """Popup reminder event; description should carry contact name, phone/email,
    and one line of context (spec §7.1)."""
    tz = _tz()
    hour = pick_reminder_hour(busy_hours_from_events(events_on(requested_date), requested_date, tz))
    start = datetime.combine(requested_date, time(hour=hour), tzinfo=tz)
    end = start + timedelta(minutes=REMINDER_LENGTH_MIN)
    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start.isoformat(), "timeZone": str(tz)},
        "end": {"dateTime": end.isoformat(), "timeZone": str(tz)},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 10}]},
    }
    svc = calendar_service(_calendar_address())
    created = svc.events().insert(calendarId="primary", body=event).execute()
    return {"event_id": created["id"], "start": created["start"]["dateTime"], "link": created.get("htmlLink")}
