"""Google service-account plumbing (domain-wide delegation).

One service account impersonates each mailbox directly (spec §5): hello@ is a
separate Workspace mailbox and is read as itself — never via to:/from: filters
inside arda@'s mailbox.
"""

import json
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build

from app.settings import get_settings

# gmail.compose technically permits sending; the never-send guarantee is
# enforced in app.tools.gmail, which exposes draft functions only (spec §5).
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.events",
]


def sa_configured() -> bool:
    return bool(get_settings().google_sa_json)


def _load_sa_info() -> dict:
    raw = get_settings().google_sa_json
    if not raw:
        raise RuntimeError("GOOGLE_SA_JSON is not configured")
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    path = Path(raw)
    if path.exists():
        return json.loads(path.read_text())
    raise RuntimeError("GOOGLE_SA_JSON is neither JSON nor a readable file path")


def delegated_credentials(user_email: str) -> service_account.Credentials:
    return service_account.Credentials.from_service_account_info(
        _load_sa_info(), scopes=SCOPES, subject=user_email
    )


def gmail_service(user_email: str):
    return build(
        "gmail", "v1", credentials=delegated_credentials(user_email), cache_discovery=False
    )


def calendar_service(user_email: str):
    return build(
        "calendar", "v3", credentials=delegated_credentials(user_email), cache_discovery=False
    )
