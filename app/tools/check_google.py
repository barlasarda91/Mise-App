"""Connectivity test for the Google connectors (spec §13 milestone 3).

Run:  python -m app.tools.check_google

Verifies, per connector, that the service account can impersonate the mailbox
and read it. An impersonation failure on hello@ typically means it isn't a
licensed user seat or delegation isn't authorized (spec §5 provisioning caveat).
"""

import sys
from datetime import datetime, timedelta, timezone

from app.models.enums import FromMailbox
from app.settings import get_settings
from app.tools.google_client import calendar_service, gmail_service, sa_configured

NOT_CONFIGURED = "not_configured"


def _check_mailbox(address: str) -> dict:
    svc = gmail_service(address)
    profile = svc.users().getProfile(userId="me").execute()
    sent = svc.users().messages().list(userId="me", q="in:sent", maxResults=1).execute()
    return {
        "status": "ok",
        "detail": f"{profile['emailAddress']} · {profile.get('messagesTotal', '?')} messages"
        + (" · sent readable" if sent.get("messages") is not None else ""),
    }


def _check_calendar(address: str) -> dict:
    svc = calendar_service(address)
    now = datetime.now(timezone.utc)
    events = (
        svc.events()
        .list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=7)).isoformat(),
            singleEvents=True,
            maxResults=5,
        )
        .execute()
    )
    return {"status": "ok", "detail": f"{len(events.get('items', []))} events next 7 days"}


def connectivity_report() -> dict[str, dict]:
    settings = get_settings()
    connectors = {
        "gmail_arda": (settings.gmail_arda_address, _check_mailbox),
        "gmail_hello": (settings.gmail_hello_address, _check_mailbox),
        "calendar": (settings.calendar_address or settings.gmail_arda_address, _check_calendar),
    }
    report: dict[str, dict] = {}
    for key, (address, check) in connectors.items():
        if not sa_configured():
            report[key] = {"status": NOT_CONFIGURED, "detail": "GOOGLE_SA_JSON not set", "address": address}
            continue
        try:
            report[key] = {**check(address), "address": address}
        except Exception as exc:
            report[key] = {"status": "error", "detail": f"{type(exc).__name__}: {exc}", "address": address}
    return report


def main() -> int:
    report = connectivity_report()
    failed = False
    for key, entry in report.items():
        mark = {"ok": "✅", NOT_CONFIGURED: "⚪"}.get(entry["status"], "❌")
        print(f"{mark} {key:<12} {entry['address']:<32} {entry['status']}: {entry['detail']}")
        failed = failed or entry["status"] == "error"
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
