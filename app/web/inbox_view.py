"""Inbox tab: merged unread mail from both mailboxes, sender muting, and a
cached unread count for the nav badge.

Scope note: Mise reads mail and drafts replies; it cannot mark messages read
(gmail.readonly + gmail.compose only) — unread state clears in Gmail.
"""

import time
from email.utils import parseaddr, parsedate_to_datetime

from sqlalchemy import select

from app.db import db_session
from app.models import MutedSender
from app.models.enums import FromMailbox
from app.tools.google_client import sa_configured

MAX_PER_MAILBOX = 25
MAX_MUTED_IN_QUERY = 40
COUNT_TTL_SECONDS = 60

_count_cache: dict = {"at": 0.0, "value": None}


def muted_list() -> list[str]:
    try:
        with db_session() as s:
            return list(s.scalars(select(MutedSender.email).order_by(MutedSender.email)))
    except Exception:
        return []


def mute_sender(email: str) -> str:
    email = email.strip().lower()
    if "@" not in email:
        return "Not a valid address."
    with db_session() as s:
        if s.scalar(select(MutedSender).where(MutedSender.email == email)):
            return f"{email} was already muted."
        s.add(MutedSender(email=email))
    _count_cache["at"] = 0  # recount on next load
    return f"Muted {email} — their mail won't show in Mise again."


def unmute_sender(email: str) -> str:
    email = email.strip().lower()
    with db_session() as s:
        row = s.scalar(select(MutedSender).where(MutedSender.email == email))
        if row is None:
            return "Not muted."
        s.delete(row)
    _count_cache["at"] = 0
    return f"Unmuted {email}."


def unread_query(muted: list[str]) -> str:
    negations = " ".join(f"-from:{m}" for m in muted[:MAX_MUTED_IN_QUERY])
    return f"in:inbox is:unread {negations}".strip()


def unread_count() -> int | None:
    """Total unread across both mailboxes, muted senders excluded. Cached
    briefly so the nav badge doesn't slow every page; None when unavailable."""
    if not sa_configured():
        return None
    now = time.time()
    if now - _count_cache["at"] < COUNT_TTL_SECONDS:
        return _count_cache["value"]
    from app.tools import gmail

    query = unread_query(muted_list())
    try:
        total = sum(gmail.count_messages(mb, query) for mb in (FromMailbox.ARDA, FromMailbox.HELLO))
    except Exception:
        total = None
    _count_cache.update(at=now, value=total)
    return total


def _stamp(entry: dict) -> float:
    try:
        return parsedate_to_datetime(entry.get("date", "")).timestamp()
    except Exception:
        return 0.0


def load_inbox() -> dict:
    """Merged unread list, newest first, muted senders excluded."""
    if not sa_configured():
        return {"error": "Google connector not configured.", "messages": []}
    from app.tools import gmail

    muted = set(muted_list())
    query = unread_query(list(muted))
    merged, errors = [], []
    for mailbox in (FromMailbox.ARDA, FromMailbox.HELLO):
        try:
            hits = gmail.search_messages(mailbox, query, max_results=MAX_PER_MAILBOX)
        except Exception as exc:
            errors.append(f"{mailbox.value}: {type(exc).__name__}: {exc}")
            continue
        for hit in hits:
            addr = parseaddr(hit.get("from", ""))[1].lower()
            if addr in muted:  # belt to the query's suspenders
                continue
            merged.append(
                {
                    "mailbox": mailbox.value,
                    "id": hit["id"],
                    "thread_id": hit.get("thread_id"),
                    "from": hit.get("from", ""),
                    "from_addr": addr,
                    "subject": hit.get("subject") or "(no subject)",
                    "date": hit.get("date", ""),
                    "snippet": hit.get("snippet", ""),
                }
            )
    merged.sort(key=_stamp, reverse=True)
    return {"error": "; ".join(errors) if errors else None, "messages": merged}


def load_open_message(mailbox: str, msg_id: str) -> dict | None:
    try:
        from app.tools import gmail

        message = gmail.get_message(FromMailbox(mailbox), msg_id)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "error": None,
        "mailbox": mailbox,
        "id": message["id"],
        "thread_id": message.get("thread_id"),
        "from": message.get("from", ""),
        "from_addr": parseaddr(message.get("from", ""))[1].lower(),
        "to": message.get("to", ""),
        "cc": message.get("cc", ""),
        "subject": message.get("subject") or "(no subject)",
        "date": message.get("date", ""),
        "body": message.get("body") or message.get("snippet", ""),
    }
