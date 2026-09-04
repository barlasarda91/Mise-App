"""Calendar page: today's and tomorrow's program from arda's calendar."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.settings import get_settings
from app.tools.google_client import sa_configured

DAYS = 2  # today + tomorrow


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().default_tz)


def _parse_point(value: dict, tz: ZoneInfo, end: bool = False) -> tuple[datetime, bool]:
    """Google event start/end -> (aware local datetime, is_all_day)."""
    if value.get("dateTime"):
        return datetime.fromisoformat(value["dateTime"]).astimezone(tz), False
    day = date.fromisoformat(value["date"])
    if end:  # all-day end dates are exclusive
        day -= timedelta(days=1)
    return datetime.combine(day, time.min if not end else time.max, tzinfo=tz), True


def shape_days(events: list[dict], today: date, tz: ZoneInfo, days: int = DAYS) -> list[dict]:
    """Group events into consecutive day buckets, all-day first then by time."""
    out = []
    for offset in range(days):
        day = today + timedelta(days=offset)
        label = "Today" if offset == 0 else ("Tomorrow" if offset == 1 else day.strftime("%A"))
        bucket = {"label": label, "date": day.strftime("%a %d %b").upper(), "events": []}
        for event in events:
            try:
                start, all_day = _parse_point(event.get("start") or {}, tz)
                end, _ = _parse_point(event.get("end") or {}, tz, end=True)
            except Exception:
                continue
            if not (start.date() <= day <= end.date()):
                continue
            tz_label = (event.get("start") or {}).get("timeZone")
            bucket["events"].append(
                {
                    "all_day": all_day,
                    "time": "ALL DAY" if all_day else f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}",
                    "sort": (0 if all_day else 1, start),
                    "summary": event.get("summary") or "(no title)",
                    "location": event.get("location"),
                    "attendees": len(event.get("attendees") or []),
                    "tz_mismatch": bool(tz_label and not all_day and tz_label != str(tz)),
                }
            )
        bucket["events"].sort(key=lambda e: e.pop("sort"))
        out.append(bucket)
    return out


def load_calendar() -> dict:
    if not sa_configured():
        return {"error": "Google connector not configured.", "days": []}
    from app.tools.calendar import list_events

    tz = _tz()
    today = datetime.now(tz).date()
    start = datetime.combine(today, time.min, tzinfo=tz)
    try:
        events = list_events(start, start + timedelta(days=DAYS))
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "days": []}
    return {"error": None, "days": shape_days(events, today, tz)}
