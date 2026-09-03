"""QuickBooks Online (A/R only — spec §5/§8.1; A/P is Wolverine's, untouched).

Token handling (2026-08-27 review): Intuit rotates the refresh token on use.
The env var QBO_REFRESH_TOKEN only seeds the first refresh; every rotated
token is persisted to app_state BEFORE the access token is used, so a crash
can't strand us with a dead token.
"""

import base64
import logging
from datetime import date

import requests
from sqlalchemy import select

from app.db import db_session
from app.models import AppState
from app.settings import get_settings

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
SCOPE = "com.intuit.quickbooks.accounting"
STATE_KEY = "qbo_tokens"
TIMEOUT = 30


def configured() -> bool:
    s = get_settings()
    return bool(s.qbo_client_id and s.qbo_client_secret)


def api_base() -> str:
    if get_settings().qbo_environment == "sandbox":
        return "https://sandbox-quickbooks.api.intuit.com"
    return "https://quickbooks.api.intuit.com"


def authorize_url(redirect_uri: str, state: str) -> str:
    s = get_settings()
    from urllib.parse import urlencode

    return AUTH_URL + "?" + urlencode(
        {
            "client_id": s.qbo_client_id,
            "response_type": "code",
            "scope": SCOPE,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )


def _basic_auth() -> dict:
    s = get_settings()
    creds = base64.b64encode(f"{s.qbo_client_id}:{s.qbo_client_secret}".encode()).decode()
    return {"Authorization": f"Basic {creds}", "Accept": "application/json"}


def _load_tokens(session_factory=db_session) -> dict | None:
    with session_factory() as s:
        row = s.scalar(select(AppState).where(AppState.key == STATE_KEY))
        if row is not None:
            return dict(row.value)
    seed = get_settings().qbo_refresh_token
    if seed:
        return {"refresh_token": seed, "realm_id": get_settings().qbo_realm_id}
    return None


def _save_tokens(refresh_token: str, realm_id: str | None, session_factory=db_session) -> None:
    with session_factory() as s:
        row = s.scalar(select(AppState).where(AppState.key == STATE_KEY))
        value = {"refresh_token": refresh_token, "realm_id": realm_id}
        if row is None:
            s.add(AppState(key=STATE_KEY, value=value))
        else:
            row.value = value


def exchange_code(code: str, redirect_uri: str, realm_id: str, session_factory=db_session) -> None:
    """OAuth callback: swap the authorization code for tokens and persist."""
    resp = requests.post(
        TOKEN_URL,
        headers=_basic_auth(),
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    _save_tokens(data["refresh_token"], realm_id, session_factory)


def get_access_token(session_factory=db_session) -> tuple[str, str]:
    """Refresh -> (access_token, realm_id). Persists the rotated refresh token
    before returning."""
    if not configured():
        raise RuntimeError("QuickBooks is not configured (QBO_CLIENT_ID/SECRET missing)")
    tokens = _load_tokens(session_factory)
    if tokens is None or not tokens.get("refresh_token"):
        raise RuntimeError("QuickBooks is not connected — use Connect on the Settings page")
    resp = requests.post(
        TOKEN_URL,
        headers=_basic_auth(),
        data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    realm_id = tokens.get("realm_id") or get_settings().qbo_realm_id
    # Persist the rotated token FIRST — losing it locks us out (spec §5).
    _save_tokens(data.get("refresh_token") or tokens["refresh_token"], realm_id, session_factory)
    if not realm_id:
        raise RuntimeError("QuickBooks realm id unknown — reconnect from Settings")
    return data["access_token"], realm_id


def _query(sql: str, session_factory=db_session) -> dict:
    access_token, realm_id = get_access_token(session_factory)
    resp = requests.get(
        f"{api_base()}/v3/company/{realm_id}/query",
        params={"query": sql, "minorversion": "75"},
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def parse_overdue_invoices(payload: dict, today: date) -> list[dict]:
    """Pure: QBO query payload -> overdue receivables (balance > 0, due < today)."""
    out = []
    for inv in (payload.get("QueryResponse") or {}).get("Invoice") or []:
        balance = float(inv.get("Balance") or 0)
        due_raw = inv.get("DueDate")
        if balance <= 0 or not due_raw:
            continue
        due = date.fromisoformat(due_raw)
        if due >= today:
            continue
        out.append(
            {
                "invoice_id": inv.get("Id"),
                "doc_number": inv.get("DocNumber"),
                "customer": (inv.get("CustomerRef") or {}).get("name"),
                "total": float(inv.get("TotalAmt") or 0),
                "balance": balance,
                "due_date": due_raw,
                "days_overdue": (today - due).days,
            }
        )
    out.sort(key=lambda i: i["days_overdue"], reverse=True)
    return out


def list_overdue_invoices(today: date, session_factory=db_session) -> list[dict]:
    payload = _query("SELECT * FROM Invoice WHERE Balance > '0' MAXRESULTS 1000", session_factory)
    return parse_overdue_invoices(payload, today)


def qbo_status(session_factory=db_session) -> dict:
    """Settings-page health row."""
    if not configured():
        return {"status": "not_configured", "detail": "QBO_CLIENT_ID / QBO_CLIENT_SECRET not set"}
    try:
        tokens = _load_tokens(session_factory)
    except Exception as exc:
        return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}
    if tokens is None or not tokens.get("refresh_token"):
        return {"status": "not_connected", "detail": "authorize with Connect to store tokens"}
    try:
        access_token, realm_id = get_access_token(session_factory)
        resp = requests.get(
            f"{api_base()}/v3/company/{realm_id}/companyinfo/{realm_id}",
            params={"minorversion": "75"},
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        name = resp.json().get("CompanyInfo", {}).get("CompanyName", "?")
        return {"status": "ok", "detail": f"{name} · realm {realm_id}"}
    except Exception as exc:
        return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}
