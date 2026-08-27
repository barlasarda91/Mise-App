import base64
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.models.enums import FromMailbox
from app.tools.calendar import busy_hours_from_events, pick_reminder_hour
from app.tools.check_google import connectivity_report
from app.tools.gmail import build_mime, decode_mime, extract_body, gmail_query, mailbox_address

LA = ZoneInfo("America/Los_Angeles")


def test_mailbox_addresses():
    assert mailbox_address(FromMailbox.ARDA) == "ardabarlas@boxxcoffee.com"
    assert mailbox_address(FromMailbox.HELLO) == "hello@boxxcoffee.com"


def test_gmail_query_incremental_bound():
    after = datetime(2026, 8, 26, 8, 30, tzinfo=timezone.utc)
    assert gmail_query("in:sent", after) == f"in:sent after:{int(after.timestamp())}"
    assert gmail_query("in:sent") == "in:sent"


def test_build_mime_roundtrip():
    raw = build_mime(
        "ardabarlas@boxxcoffee.com",
        ["deniz@emberroom.la"],
        "Boxx wholesale — agreement",
        "Hey Deniz,\n\nBest\nArda",
        cc=["hello@boxxcoffee.com"],
    )
    msg = decode_mime(raw)
    assert msg["From"] == "ardabarlas@boxxcoffee.com"
    assert msg["To"] == "deniz@emberroom.la"
    assert msg["Cc"] == "hello@boxxcoffee.com"
    assert msg["Subject"] == "Boxx wholesale — agreement"
    assert "Best\nArda" in msg.get_content()
    assert msg["In-Reply-To"] is None


def test_build_mime_reply_headers():
    raw = build_mime(
        "hello@boxxcoffee.com",
        ["lead@halcyon.la"],
        "Re: Wholesale pricing",
        "body",
        in_reply_to="<abc@mail.gmail.com>",
        references="<first@mail.gmail.com>",
    )
    msg = decode_mime(raw)
    assert msg["In-Reply-To"] == "<abc@mail.gmail.com>"
    assert msg["References"] == "<first@mail.gmail.com> <abc@mail.gmail.com>"


def _part(mime, text):
    return {"mimeType": mime, "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}


def test_extract_body_prefers_plain_text():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            _part("text/plain", "plain body"),
            _part("text/html", "<p>html body</p>"),
        ],
    }
    assert extract_body(payload) == "plain body"


def test_extract_body_falls_back_to_stripped_html():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {
                "mimeType": "multipart/related",
                "parts": [_part("text/html", "<style>x{}</style><p>Hey Deniz,</p><br>Best")],
            }
        ],
    }
    text = extract_body(payload)
    assert "Hey Deniz," in text and "Best" in text and "<p>" not in text


def test_pick_reminder_hour_prefers_nine_then_ten_eleven():
    assert pick_reminder_hour(set()) == 9
    assert pick_reminder_hour({9}) == 10
    assert pick_reminder_hour({9, 10}) == 11
    assert pick_reminder_hour({9, 10, 11}) == 9  # all taken -> overlap at 9


def _event(start_iso, end_iso):
    return {"start": {"dateTime": start_iso}, "end": {"dateTime": end_iso}}


def test_busy_hours_from_events():
    day = date(2026, 8, 28)
    events = [
        _event("2026-08-28T09:00:00-07:00", "2026-08-28T10:30:00-07:00"),  # blocks 9, 10
        {"start": {"date": "2026-08-28"}, "end": {"date": "2026-08-29"}},  # all-day: ignored
    ]
    busy = busy_hours_from_events(events, day, LA)
    assert busy == {9, 10}
    assert pick_reminder_hour(busy) == 11


def test_connectivity_report_without_credentials():
    report = connectivity_report()
    assert set(report) == {"gmail_arda", "gmail_hello", "calendar"}
    for entry in report.values():
        assert entry["status"] == "not_configured"


def test_no_send_function_in_tool_layer():
    """The never-send guarantee is structural (spec §5): the Gmail tool module
    must not grow a send capability."""
    import app.tools.gmail as gmail_tools

    exported = [name for name in dir(gmail_tools) if "send" in name.lower()]
    assert exported == []
    import inspect

    assert "messages().send" not in inspect.getsource(gmail_tools)
