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


def awaiting_list(limit: int = 80) -> list[dict]:
    """The mail index's awaiting-reply threads, for the Inbox tab panel —
    the same list the agenda triages, made visible and one-click actionable."""
    try:
        from app.tools.mail_index import awaiting_reply

        with db_session() as s:
            return awaiting_reply(s, limit=limit)
    except Exception:
        return []


def _dismiss_thread(session, mailbox: str, thread_id: str) -> None:
    """Upsert a Done-mark: the thread stays off the awaiting list unless the
    sender writes again after this moment."""
    from datetime import datetime, timezone

    from app.models import AwaitingDismissal

    row = session.scalar(
        select(AwaitingDismissal).where(
            AwaitingDismissal.mailbox == mailbox, AwaitingDismissal.thread_id == thread_id
        )
    )
    now = datetime.now(timezone.utc)
    if row is None:
        session.add(AwaitingDismissal(mailbox=mailbox, thread_id=thread_id, dismissed_at=now))
    else:
        row.dismissed_at = now


def mark_awaiting_done(mailbox: str, thread_id: str, label: str = "") -> str:
    if not thread_id:
        return "That row has no thread id — re-run the sweep and try again."
    with db_session() as s:
        _dismiss_thread(s, mailbox, thread_id)
    who = f" — {label}" if label else ""
    return f"Marked done{who}. It reappears only if they write again."


MAX_BATCH = 100


def batch_awaiting(action: str, sels: list[str]) -> str:
    """Batch Done / batch → task over checked awaiting rows. The form carries
    only 'mailbox:msg_id' refs; everything else (sender, subject, thread) is
    re-derived from the mail index so stale form data can't mislabel a task."""
    from app.models import MailMessage

    if action not in ("done", "task"):
        return "Unknown batch action."
    refs = []
    for sel in sels[:MAX_BATCH]:
        mailbox, _, msg_id = sel.partition(":")
        if mailbox and msg_id:
            refs.append((mailbox, msg_id))
    if not refs:
        return "Nothing selected — tick some rows first."

    rows: list[dict] = []
    with db_session() as s:
        for mailbox, msg_id in refs:
            row = s.scalar(
                select(MailMessage).where(
                    MailMessage.mailbox == mailbox, MailMessage.gmail_msg_id == msg_id
                )
            )
            if row is not None:
                rows.append(
                    {
                        "mailbox": row.mailbox,
                        "msg_id": row.gmail_msg_id,
                        "from_name": row.from_name or row.from_addr or "",
                        "from_addr": row.from_addr or "",
                        "subject": row.subject or "",
                        # same fallback the awaiting query keys threads by
                        "thread_id": row.thread_id or row.gmail_msg_id,
                    }
                )
    skipped = len(refs) - len(rows)
    if action == "done":
        with db_session() as s:
            for r in rows:
                _dismiss_thread(s, r["mailbox"], r["thread_id"])
        note = f" ({skipped} not found in the index)" if skipped else ""
        return f"Marked {len(rows)} done{note}. Each reappears only if that sender writes again."

    created = already = linked = 0
    for r in rows:
        msg = make_task_from_thread(
            r["mailbox"], r["msg_id"], r["from_name"], r["from_addr"], r["subject"], r["thread_id"]
        )
        if "Already on the board" in msg:
            already += 1
        elif "Task created" in msg:
            created += 1
            if "Auto-linked" in msg:
                linked += 1
    parts = [f"{created} task(s) created"]
    if linked:
        parts.append(f"{linked} lead-linked")
    if already:
        parts.append(f"{already} already on the board")
    if skipped:
        parts.append(f"{skipped} not found in the index")
    return "Batch: " + ", ".join(parts) + "."


def make_task_from_thread(
    mailbox: str, msg_id: str, from_name: str, from_addr: str, subject: str, thread_id: str = ""
) -> str:
    """One-click 'turn this hanging thread into a board task' — deduped per
    message, linked to a lead when the sender matches one. Also Done-marks
    the thread on the awaiting list: it's on the board now."""
    from app.models import ExternalMutation, MutationKind, Task, TaskActivity, TaskCategory, TaskSource

    dedup_key = f"task:{msg_id}"
    title = f"Reply to {from_name or from_addr} — {subject or '(no subject)'}"[:300]
    with db_session() as s:
        ledger = s.scalar(
            select(ExternalMutation).where(ExternalMutation.dedup_key == dedup_key)
        )
        if ledger and ledger.external_id:
            existing = s.get(Task, int(ledger.external_id))
            if existing is not None:
                if thread_id:
                    _dismiss_thread(s, mailbox, thread_id)
                return f"Already on the board: {existing.title}."
        task = Task(
            category=TaskCategory.GOVERNANCE,
            title=title,
            source=TaskSource.EMAIL,
            source_ref={"gmail_msg_id": msg_id, "contact_email": (from_addr or "").lower() or None},
        )
        s.add(task)
        s.flush()
        s.add(TaskActivity(task_id=task.id, type="created", detail="from awaiting-reply list", actor="Arda"))
        if ledger is None:
            s.add(ExternalMutation(kind=MutationKind.TASK, dedup_key=dedup_key, external_id=str(task.id)))
        else:
            ledger.external_id = str(task.id)
        # The sender's address is a stronger link signal than a name match.
        from sqlalchemy import func as sa_func

        from app.models import Lead, OPEN_LEAD_STAGES
        from app.routines.task_sync import auto_link_lead

        matched = None
        if from_addr:
            matched = s.scalar(
                select(Lead).where(
                    Lead.stage.in_(OPEN_LEAD_STAGES),
                    Lead.discarded_at.is_(None),
                    sa_func.lower(Lead.contact_email) == from_addr.lower(),
                )
            )
        if matched is not None:
            ref = dict(task.source_ref or {})
            ref["lead_id"] = matched.id
            task.source_ref = ref
            s.add(TaskActivity(task_id=task.id, type="linked",
                               detail=f"auto: linked to lead {matched.business_name} (email match)", actor="Mise"))
        else:
            matched = auto_link_lead(s, task)
        note = f" Auto-linked to {matched.business_name}." if matched else ""
        if thread_id:
            _dismiss_thread(s, mailbox, thread_id)
        task_id = task.id
    return f"Task created (#{task_id}): {title}.{note}"


def load_open_message(mailbox: str, msg_id: str) -> dict | None:
    try:
        from app.tools import gmail

        message = gmail.get_message(FromMailbox(mailbox), msg_id)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    from app.web.action_links import extract_action_links, extract_invite_event_ids

    invites = []
    try:
        from app.tools.calendar import event_rsvp_state

        for event_id in extract_invite_event_ids([message]):
            state = event_rsvp_state(event_id)
            if state is not None:
                invites.append(state)
    except Exception:
        invites = []

    return {
        "action_links": extract_action_links([message]),
        "invites": invites,
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
