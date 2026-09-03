"""Drafts page: list/editor view-models + services (spec §8.2).

From/To/Cc are editable before saving. Saving creates a native Gmail draft in
the chosen mailbox (on-thread when gmail_thread_id is set); Arda reviews and
sends from Gmail. The hub never sends.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db import db_session
from app.models import DraftStatus, EmailDraft, Lead, LeadStage, OPEN_LEAD_STAGES
from app.models.enums import FromMailbox
from app.settings import get_settings

STATUS_LABELS = {
    DraftStatus.DRAFTING: "drafting…",
    DraftStatus.COMPOSED: "ready",
    DraftStatus.SAVED_TO_GMAIL: "saved to Gmail",
    DraftStatus.DISCARDED: "discarded",
}


def _addrs(value) -> str:
    return ", ".join(value or [])


def _parse_addrs(raw: str) -> list[str]:
    return [a.strip() for a in raw.replace(";", ",").split(",") if a.strip()]


def _row(draft: EmailDraft) -> dict:
    return {
        "id": draft.id,
        "subject": draft.subject or "(no subject)",
        "mailbox": draft.from_mailbox.value,
        "status": draft.status.value,
        "status_label": "sent ✓" if draft.sent_at else STATUS_LABELS[draft.status],
        "sent": bool(draft.sent_at),
        "lead_id": draft.related_lead_id,
        "is_reply": bool(draft.gmail_thread_id),
    }


def load_drafts_index(limit: int = 40) -> list[dict]:
    try:
        with db_session() as s:
            drafts = s.scalars(
                select(EmailDraft)
                .where(EmailDraft.status != DraftStatus.DISCARDED)
                .order_by(EmailDraft.id.desc())
                .limit(limit)
            ).all()
            return [_row(d) for d in drafts]
    except Exception:
        return []


def load_draft(draft_id: int) -> dict | None:
    try:
        with db_session() as s:
            draft = s.get(EmailDraft, draft_id)
            if draft is None:
                return None
            lead_name = None
            if draft.related_lead_id:
                lead = s.get(Lead, draft.related_lead_id)
                lead_name = lead.business_name if lead else None
            return {
                **_row(draft),
                "to": _addrs(draft.to_addrs),
                "cc": _addrs(draft.cc_addrs),
                "body": draft.body or "",
                "lead_name": lead_name,
                "gmail_draft_id": draft.gmail_draft_id,
                "thread_id": draft.gmail_thread_id,
            }
    except Exception:
        return None


def open_leads_for_picker() -> list[dict]:
    try:
        with db_session() as s:
            leads = s.scalars(
                select(Lead).where(Lead.stage.in_(OPEN_LEAD_STAGES)).order_by(Lead.business_name)
            ).all()
            return [{"id": l.id, "name": l.business_name} for l in leads]
    except Exception:
        return []


THREAD_BODY_CHARS = 4000


def load_thread(selected: dict | None) -> dict | None:
    """Conversation context rendered beside the editor: the reply's own thread
    when the draft has one, otherwise the most recent Gmail conversation with
    the first To address ("recent history"). Errors degrade to a note."""
    if not selected:
        return None
    from app.tools import gmail

    mailbox = FromMailbox(selected["mailbox"])
    thread_id = selected.get("thread_id")
    label = "thread"
    try:
        if not thread_id:
            first_to = (selected.get("to") or "").split(",")[0].strip()
            if not first_to:
                return None
            hits = gmail.search_messages(mailbox, first_to, max_results=1)
            if not hits or not hits[0].get("thread_id"):
                return None  # genuinely no prior history — no panel
            thread_id = hits[0]["thread_id"]
            label = "history"
        messages = gmail.get_thread_messages(mailbox, thread_id, last_n=8)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "messages": [], "label": label}
    shaped = []
    for m in messages:
        body = (m.get("body") or m.get("snippet") or "").strip()
        truncated = len(body) > THREAD_BODY_CHARS
        shaped.append(
            {
                "from": m.get("from", ""),
                "date": m.get("date", ""),
                "subject": m.get("subject", ""),
                "body": body[:THREAD_BODY_CHARS] + ("\n… (truncated)" if truncated else ""),
            }
        )
    return {"error": None, "messages": shaped, "label": label}


# ---------- services ----------


def start_generation(instruction: str, mailbox: str, lead_id: str, thread_id: str) -> tuple[str, int | None]:
    if not instruction.strip():
        return "Tell it what to draft first.", None
    with db_session() as s:
        draft = EmailDraft(
            subject="",
            from_mailbox=FromMailbox(mailbox),
            related_lead_id=int(lead_id) if lead_id else None,
            gmail_thread_id=thread_id.strip() or None,
            status=DraftStatus.DRAFTING,
        )
        if draft.related_lead_id:
            lead = s.get(Lead, draft.related_lead_id)
            if lead and lead.contact_email:
                draft.to_addrs = [lead.contact_email]
        s.add(draft)
        s.flush()
        draft_id = draft.id
    from app.engine.drafter import spawn_draft_generation

    spawn_draft_generation(draft_id, instruction.strip())
    return "Drafting — the editor fills in when it's ready.", draft_id


def create_blank(mailbox: str) -> int:
    with db_session() as s:
        draft = EmailDraft(subject="", from_mailbox=FromMailbox(mailbox), status=DraftStatus.COMPOSED, body="")
        s.add(draft)
        s.flush()
        return draft.id


def update_fields(draft_id: int, from_mailbox: str, to: str, cc: str, subject: str, body: str) -> str:
    with db_session() as s:
        draft = s.get(EmailDraft, draft_id)
        if draft is None:
            return "Draft not found."
        if draft.sent_at:
            return "Already sent — start a new draft for a follow-up."
        draft.from_mailbox = FromMailbox(from_mailbox)
        draft.to_addrs = _parse_addrs(to)
        draft.cc_addrs = _parse_addrs(cc)
        draft.subject = subject.strip()
        draft.body = body
        if draft.status == DraftStatus.SAVED_TO_GMAIL:
            draft.status = DraftStatus.COMPOSED  # edited since last save
    return "Saved."


def _mailbox_address(mailbox: FromMailbox) -> str:
    settings = get_settings()
    return settings.gmail_arda_address if mailbox == FromMailbox.ARDA else settings.gmail_hello_address


def _sync_to_gmail(draft_id: int) -> tuple[bool, str]:
    """Create/update the native Gmail draft with the hub's latest content.
    Returns (ok, message)."""
    with db_session() as s:
        draft = s.get(EmailDraft, draft_id)
        if draft is None:
            return False, "Draft not found."
        if draft.sent_at:
            return False, "Already sent — start a new draft for a follow-up."
        if draft.status == DraftStatus.DRAFTING:
            return False, "Still generating — wait for it to finish."
        if not draft.to_addrs:
            return False, "Add a To address first."
        if not draft.subject:
            return False, "Add a subject first."
        mailbox = draft.from_mailbox
        payload = dict(
            to=list(draft.to_addrs),
            subject=draft.subject,
            body=draft.body or "",
            cc=list(draft.cc_addrs) if draft.cc_addrs else None,
            thread_id=draft.gmail_thread_id,
        )
        existing_gmail_id = draft.gmail_draft_id

    from app.tools import gmail

    try:
        if existing_gmail_id:
            result = gmail.update_draft(mailbox, existing_gmail_id, **payload)
        else:
            result = gmail.create_draft(mailbox, **payload)
    except Exception as exc:
        return False, f"Gmail error: {type(exc).__name__}: {exc}"

    with db_session() as s:
        draft = s.get(EmailDraft, draft_id)
        draft.gmail_draft_id = result["draft_id"]
        if result.get("thread_id"):
            draft.gmail_thread_id = result["thread_id"]
        draft.status = DraftStatus.SAVED_TO_GMAIL
    return True, f"Saved as a Gmail draft in {_mailbox_address(mailbox)}."


def save_to_gmail(draft_id: int) -> str:
    ok, msg = _sync_to_gmail(draft_id)
    return msg + " Review and send from Gmail, or hit Send here." if ok else msg


def send_now(draft_id: int) -> str:
    """OPERATOR send: sync the latest content to the Gmail draft, then send it.
    Only ever triggered by the Send button in the Drafts UI."""
    ok, msg = _sync_to_gmail(draft_id)
    if not ok:
        return msg

    with db_session() as s:
        draft = s.get(EmailDraft, draft_id)
        mailbox = draft.from_mailbox
        gmail_id = draft.gmail_draft_id

    from app.tools import gmail

    try:
        result = gmail.send_draft(mailbox, gmail_id)
    except Exception as exc:
        return f"Gmail error on send: {type(exc).__name__}: {exc}"

    with db_session() as s:
        draft = s.get(EmailDraft, draft_id)
        draft.sent_at = datetime.now(ZoneInfo(get_settings().default_tz))
        draft.gmail_draft_id = None  # sending consumes the Gmail draft
        if result.get("thread_id"):
            draft.gmail_thread_id = result["thread_id"]
    return f"Sent from {_mailbox_address(mailbox)}."


def discard(draft_id: int) -> str:
    with db_session() as s:
        draft = s.get(EmailDraft, draft_id)
        if draft is None:
            return "Draft not found."
        draft.status = DraftStatus.DISCARDED
    return "Discarded."
