from datetime import date
from zoneinfo import ZoneInfo

from app.web.calendar_view import load_calendar, shape_days

LA = ZoneInfo("America/Los_Angeles")
TODAY = date(2026, 9, 4)


def _event(summary, start, end, tz=None, **kw):
    e = {"summary": summary, "start": dict(start), "end": dict(end), **kw}
    if tz:
        e["start"]["timeZone"] = tz
    return e


def test_shape_days_groups_sorts_and_flags():
    events = [
        _event("Tasting — Ember Room", {"dateTime": "2026-09-04T12:00:00-07:00"},
               {"dateTime": "2026-09-04T13:00:00-07:00"}, tz="America/Los_Angeles",
               location="Roastery", attendees=[{"email": "a"}, {"email": "b"}]),
        _event("Early call", {"dateTime": "2026-09-04T09:00:00-07:00"},
               {"dateTime": "2026-09-04T09:30:00-07:00"}, tz="America/New_York"),
        _event("Pop-up (all day)", {"date": "2026-09-05"}, {"date": "2026-09-06"}),
        _event("Next week", {"dateTime": "2026-09-10T09:00:00-07:00"},
               {"dateTime": "2026-09-10T10:00:00-07:00"}),
    ]
    days = shape_days(events, TODAY, LA)
    assert [d["label"] for d in days] == ["Today", "Tomorrow"]

    today_events = days[0]["events"]
    assert [e["summary"] for e in today_events] == ["Early call", "Tasting — Ember Room"]
    assert today_events[0]["tz_mismatch"] is True  # NY label on an LA calendar
    assert today_events[1]["tz_mismatch"] is False
    assert today_events[1]["time"] == "12:00–13:00"
    assert today_events[1]["attendees"] == 2

    tomorrow = days[1]["events"]
    assert tomorrow[0]["summary"] == "Pop-up (all day)"
    assert tomorrow[0]["time"] == "ALL DAY"
    # exclusive all-day end: doesn't leak past its real last day
    assert all(e["summary"] != "Next week" for e in tomorrow)


def test_all_day_first_then_times():
    events = [
        _event("Meeting", {"dateTime": "2026-09-04T08:00:00-07:00"},
               {"dateTime": "2026-09-04T09:00:00-07:00"}),
        _event("Holiday", {"date": "2026-09-04"}, {"date": "2026-09-05"}),
    ]
    today = shape_days(events, TODAY, LA)[0]["events"]
    assert [e["time"] for e in today] == ["ALL DAY", "08:00–09:00"]


def test_load_calendar_unconfigured(monkeypatch):
    import app.web.calendar_view as cv

    monkeypatch.setattr(cv, "sa_configured", lambda: False)
    result = load_calendar()
    assert "not configured" in result["error"]
    assert result["days"] == []
