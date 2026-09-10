"""Mail index engine: 90-day backfill sweep + incremental upkeep + the
awaiting-reply query (the app's durable email memory).

Zero model tokens — everything here is Gmail metadata API + Postgres. The
backfill runs in a background thread (Settings button) and is safe to
re-run: rows upsert by (mailbox, message id).
"""

import logging
import threading
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db import db_session
from app.models import AppState, MailMessage
from app.models.enums import FromMailbox
from app.settings import get_settings

log = logging.getLogger(__name__)

BACKFILL_DAYS = 90
BACKFILL_STATE_KEY = "mail_index:backfill"
MAX_BACKFILL_PAGES = 60  # x100 messages per mailbox — a hard stop, not a target
OWN_DOMAIN = "boxxcoffee.com"


def _parse_when(value: str) -> datetime | None:
    try:
        dt = parsedate_to_datetime(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def upsert_summary(session, mailbox: str, summary: dict) -> bool:
    """Insert one header summary; returns True when it was new."""
    existing = session.scalar(
        select(MailMessage).where(
            MailMessage.mailbox == mailbox, MailMessage.gmail_msg_id == summary["id"]
        )
    )
    if existing is not None:
        return False
    name, addr = parseaddr(summary.get("from", ""))
    session.add(
        MailMessage(
            mailbox=mailbox,
            gmail_msg_id=summary["id"],
            thread_id=summary.get("thread_id"),
            from_addr=(addr or "").lower()[:320] or None,
            from_name=(name or "")[:200] or None,
            to_addrs=(summary.get("to") or "")[:1000] or None,
            subject=(summary.get("subject") or "")[:500] or None,
            snippet=(summary.get("snippet") or "")[:300] or None,
            sent_at=_parse_when(summary.get("date", "")),
            is_outbound=(addr or "").lower().endswith("@" + OWN_DOMAIN),
        )
    )
    return True


def _set_backfill_state(value: dict) -> None:
    with db_session() as s:
        row = s.get(AppState, BACKFILL_STATE_KEY)
        if row is None:
            s.add(AppState(key=BACKFILL_STATE_KEY, value=value))
        else:
            row.value = value


def backfill(days: int = BACKFILL_DAYS) -> dict:
    """Sweep both mailboxes' last N days (inbox, sent, archive — everything
    but spam/trash) into the index. Paged; progress in app_state."""
    from app.tools import gmail

    totals = {"indexed": 0, "seen": 0}
    _set_backfill_state({"state": "running", "started": datetime.now(timezone.utc).isoformat(), **totals})
    try:
        for mailbox in (FromMailbox.ARDA, FromMailbox.HELLO):
            token = None
            for _ in range(MAX_BACKFILL_PAGES):
                summaries, token = gmail.search_page(
                    mailbox, f"newer_than:{days}d -in:chats", page_token=token
                )
                with db_session() as s:
                    for summary in summaries:
                        totals["seen"] += 1
                        if upsert_summary(s, mailbox.value, summary):
                            totals["indexed"] += 1
                _set_backfill_state({"state": "running", "mailbox": mailbox.value, **totals})
                if not token:
                    break
        _set_backfill_state(
            {"state": "done", "finished": datetime.now(timezone.utc).isoformat(), **totals}
        )
    except Exception as exc:
        log.exception("mail index backfill failed")
        _set_backfill_state({"state": "error", "error": f"{type(exc).__name__}: {exc}", **totals})
    return totals


def start_backfill() -> str:
    with db_session() as s:
        row = s.get(AppState, BACKFILL_STATE_KEY)
        if row is not None and (row.value or {}).get("state") == "running":
            return "A backfill is already running — watch its progress here."
    threading.Thread(target=backfill, daemon=True).start()
    return "90-day mail sweep started in the background — refresh this page for progress."


def index_recent(mailbox: FromMailbox, overlap_hours: int = 26) -> int:
    """Incremental upkeep: index mail newer than the latest indexed message
    (minus an overlap; upserts make re-seeing safe). Cheap when current."""
    from app.tools import gmail

    with db_session() as s:
        newest = s.scalar(
            select(MailMessage.sent_at)
            .where(MailMessage.mailbox == mailbox.value, MailMessage.sent_at.isnot(None))
            .order_by(MailMessage.sent_at.desc())
            .limit(1)
        )
    if newest is None:
        return 0  # no index yet — the backfill builds it, not the hourly path
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    since = int((newest - timedelta(hours=overlap_hours)).timestamp())
    added = 0
    token = None
    for _ in range(5):  # up to 500 new messages per upkeep pass
        summaries, token = gmail.search_page(
            mailbox, f"after:{since} -in:chats", page_token=token
        )
        with db_session() as s:
            for summary in summaries:
                if upsert_summary(s, mailbox.value, summary):
                    added += 1
        if not token:
            break
    return added


def awaiting_reply(session, limit: int = 15, max_age_days: int = BACKFILL_DAYS) -> list[dict]:
    """Threads whose newest indexed message is inbound with no Boxx reply
    after it — the read-or-unread 'left hanging' list. Muted and disregarded
    senders are excluded."""
    from app.models import DisregardRule, MutedSender

    tz = ZoneInfo(get_settings().default_tz)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    excluded = {e.lower() for e in session.scalars(select(MutedSender.email))}
    excluded |= {
        r.contact_email for r in session.scalars(select(DisregardRule)) if r.contact_email
    }

    rows = session.scalars(
        select(MailMessage).where(MailMessage.sent_at.isnot(None)).order_by(MailMessage.sent_at)
    ).all()
    threads: dict = {}
    for row in rows:
        sent = row.sent_at if row.sent_at.tzinfo else row.sent_at.replace(tzinfo=timezone.utc)
        if sent < cutoff:
            continue
        threads[(row.mailbox, row.thread_id or row.gmail_msg_id)] = (row, sent)

    now = datetime.now(timezone.utc)
    out = []
    for row, sent in threads.values():
        if row.is_outbound or not row.from_addr:
            continue
        if row.from_addr in excluded or "noreply" in row.from_addr or "no-reply" in row.from_addr:
            continue
        out.append(
            {
                "mailbox": row.mailbox,
                "thread_id": row.thread_id,
                "gmail_msg_id": row.gmail_msg_id,
                "from_name": row.from_name or row.from_addr,
                "from_addr": row.from_addr,
                "subject": row.subject or "(no subject)",
                "snippet": row.snippet or "",
                "last_message": sent.astimezone(tz).strftime("%Y-%m-%d"),
                "age_days": (now - sent).days,
            }
        )
    out.sort(key=lambda t: t["age_days"], reverse=True)
    return out[:limit]


def index_status() -> dict:
    out = {"rows": 0, "per_mailbox": {}, "backfill": None}
    try:
        with db_session() as s:
            for mailbox in ("arda", "hello"):
                newest = s.scalar(
                    select(MailMessage.sent_at)
                    .where(MailMessage.mailbox == mailbox, MailMessage.sent_at.isnot(None))
                    .order_by(MailMessage.sent_at.desc())
                    .limit(1)
                )
                count = len(s.scalars(
                    select(MailMessage.id).where(MailMessage.mailbox == mailbox)
                ).all())
                out["per_mailbox"][mailbox] = {"rows": count, "newest": newest}
                out["rows"] += count
            state = s.get(AppState, BACKFILL_STATE_KEY)
            out["backfill"] = state.value if state else None
    except Exception:
        pass
    return out
