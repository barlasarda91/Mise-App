"""Drafts page: list/editor view-models + services (spec §8.2).

From/To/Cc are editable before saving. Saving creates a native Gmail draft in
the chosen mailbox (on-thread when gmail_thread_id is set); Arda reviews and
sends from Gmail. The hub never sends.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.db import db_session
from app.models import (
    ActivitySource,
    DraftAttachment,
    DraftStatus,
    EmailDraft,
    Lead,
    LeadActivity,
    LeadActivityType,
    LeadStage,
    OPEN_LEAD_STAGES,
    StoredFile,
)
from app.models.enums import FromMailbox
from app.settings import get_settings

MAX_FILE_BYTES = 10 * 1024 * 1024  # per file
MAX_TOTAL_ATTACH_BYTES = 20 * 1024 * 1024  # per email


def _fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{max(n // 1024, 1)} KB"

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
            attachments = [
                {"id": att_id, "filename": filename, "size": _fmt_size(size)}
                for att_id, filename, size in s.execute(
                    select(DraftAttachment.id, StoredFile.filename, StoredFile.size)
                    .join(StoredFile, DraftAttachment.file_id == StoredFile.id)
                    .where(DraftAttachment.draft_id == draft_id)
                    .order_by(DraftAttachment.id)
                ).all()
            ]
            return {
                **_row(draft),
                "to": _addrs(draft.to_addrs),
                "cc": _addrs(draft.cc_addrs),
                "body": draft.body or "",
                "lead_name": lead_name,
                "gmail_draft_id": draft.gmail_draft_id,
                "thread_id": draft.gmail_thread_id,
                "attachments": attachments,
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


def library_files() -> list[dict]:
    try:
        with db_session() as s:
            files = s.execute(
                select(StoredFile.id, StoredFile.filename, StoredFile.label, StoredFile.size)
                .where(StoredFile.in_library.is_(True))
                .order_by(StoredFile.filename)
            ).all()
            return [
                {"id": f.id, "name": f.label or f.filename, "size": _fmt_size(f.size)}
                for f in files
            ]
    except Exception:
        return []


def attach_upload(draft_id: int, filename: str, content: bytes, content_type: str, to_library: bool, label: str) -> str:
    if not filename or not content:
        return "Choose a file first."
    if len(content) > MAX_FILE_BYTES:
        return f"File too large ({_fmt_size(len(content))}) — 10 MB max."
    with db_session() as s:
        draft = s.get(EmailDraft, draft_id)
        if draft is None:
            return "Draft not found."
        if draft.sent_at:
            return "Already sent."
        stored = StoredFile(
            filename=filename,
            content_type=content_type or "application/octet-stream",
            size=len(content),
            data=content,
            in_library=to_library,
            label=label.strip() or None if to_library else None,
        )
        s.add(stored)
        s.flush()
        s.add(DraftAttachment(draft_id=draft_id, file_id=stored.id))
        if draft.status == DraftStatus.SAVED_TO_GMAIL:
            draft.status = DraftStatus.COMPOSED  # changed since last save
    saved = " (kept in library)" if to_library else ""
    return f"Attached {filename}{saved}."


def attach_from_library(draft_id: int, file_id: int) -> str:
    with db_session() as s:
        draft = s.get(EmailDraft, draft_id)
        stored = s.get(StoredFile, file_id)
        if draft is None or stored is None:
            return "Not found."
        if draft.sent_at:
            return "Already sent."
        exists = s.scalar(
            select(DraftAttachment).where(
                DraftAttachment.draft_id == draft_id, DraftAttachment.file_id == file_id
            )
        )
        if exists:
            return "Already attached."
        s.add(DraftAttachment(draft_id=draft_id, file_id=file_id))
        if draft.status == DraftStatus.SAVED_TO_GMAIL:
            draft.status = DraftStatus.COMPOSED
        name = stored.label or stored.filename
    return f"Attached {name}."


def remove_attachment(draft_id: int, attachment_id: int) -> str:
    with db_session() as s:
        att = s.get(DraftAttachment, attachment_id)
        if att is None or att.draft_id != draft_id:
            return "Not found."
        draft = s.get(EmailDraft, draft_id)
        if draft and draft.sent_at:
            return "Already sent."
        stored = s.get(StoredFile, att.file_id)
        s.delete(att)
        # ad-hoc uploads (not library) are orphaned once detached — clean up
        if stored and not stored.in_library:
            others = s.scalar(select(DraftAttachment).where(DraftAttachment.file_id == stored.id))
            if others is None:
                s.delete(stored)
    return "Removed."


def _load_attachment_payloads(session, draft_id: int) -> list[dict]:
    rows = session.execute(
        select(StoredFile)
        .join(DraftAttachment, DraftAttachment.file_id == StoredFile.id)
        .where(DraftAttachment.draft_id == draft_id)
        .order_by(DraftAttachment.id)
    ).scalars().all()
    return [
        {"filename": f.filename, "content_type": f.content_type, "data": f.data}
        for f in rows
    ]


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
        return {"error": f"{type(exc).__name__}: {exc}", "messages": [], "label": label,
                "thread_id": thread_id, "mailbox": selected["mailbox"]}
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
    return {"error": None, "messages": shaped, "label": label,
            "thread_id": thread_id, "mailbox": selected["mailbox"]}


# ---------- services ----------


def start_generation(
    instruction: str, mailbox: str, lead_id: str, thread_id: str,
    task_id: str = "", to: str = "",
) -> tuple[str, int | None]:
    if not instruction.strip():
        return "Tell it what to draft first.", None
    with db_session() as s:
        draft = EmailDraft(
            subject="",
            from_mailbox=FromMailbox(mailbox),
            related_lead_id=int(lead_id) if lead_id else None,
            related_task_id=int(task_id) if task_id else None,
            gmail_thread_id=thread_id.strip() or None,
            status=DraftStatus.DRAFTING,
        )
        if draft.related_lead_id:
            lead = s.get(Lead, draft.related_lead_id)
            if lead and lead.contact_email:
                draft.to_addrs = [lead.contact_email]
        if not draft.to_addrs and to.strip():
            draft.to_addrs = [to.strip()]
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
        attachments = _load_attachment_payloads(s, draft_id)
        total = sum(len(a["data"]) for a in attachments)
        if total > MAX_TOTAL_ATTACH_BYTES:
            return False, f"Attachments total {_fmt_size(total)} — 20 MB max per email."
        payload = dict(
            to=list(draft.to_addrs),
            subject=draft.subject,
            body=draft.body or "",
            cc=list(draft.cc_addrs) if draft.cc_addrs else None,
            thread_id=draft.gmail_thread_id,
            attachments=attachments or None,
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

    lead_note = ""
    with db_session() as s:
        draft = s.get(EmailDraft, draft_id)
        now = datetime.now(ZoneInfo(get_settings().default_tz))
        draft.sent_at = now
        draft.gmail_draft_id = None  # sending consumes the Gmail draft
        if result.get("thread_id"):
            draft.gmail_thread_id = result["thread_id"]

        # Operator-sent email against a linked lead: log the activity (keyed
        # by the real Gmail message id so the morning audit is idempotent),
        # reset the idle timer, and auto-advance New -> Contacted.
        lead = s.get(Lead, draft.related_lead_id) if draft.related_lead_id else None
        if lead is None and draft.to_addrs:
            # Unlinked draft: match the To address against open leads so the
            # pipeline still advances (and adopt the link for the record).
            first_to = draft.to_addrs[0].strip().lower()
            lead = s.scalar(
                select(Lead).where(
                    Lead.stage.in_(OPEN_LEAD_STAGES),
                    func.lower(Lead.contact_email) == first_to,
                )
            )
            if lead is not None:
                draft.related_lead_id = lead.id
        if lead is not None:
            today = now.date()
            already = s.scalar(
                select(LeadActivity).where(LeadActivity.gmail_msg_id == result.get("message_id"))
            )
            if not already:
                s.add(
                    LeadActivity(
                        lead_id=lead.id,
                        type=LeadActivityType.EMAIL_SENT,
                        occurred_on=today,
                        detail=draft.subject,
                        source=ActivitySource.GMAIL,
                        gmail_msg_id=result.get("message_id"),
                    )
                )
            if lead.last_confirmed_action is None or today > lead.last_confirmed_action:
                lead.last_confirmed_action = today
            if lead.stage == LeadStage.NEW:
                s.add(
                    LeadActivity(
                        lead_id=lead.id,
                        type=LeadActivityType.STAGE_CHANGE,
                        occurred_on=today,
                        detail="new → contacted (email sent from hub)",
                        source=ActivitySource.MANUAL,
                    )
                )
                lead.stage = LeadStage.CONTACTED
                lead.stage_since = today
                lead_note = f" {lead.business_name} → contacted, timer reset."
            else:
                lead_note = f" {lead.business_name}: timer reset to today."
            from app.routines.task_sync import sync_lead_tasks

            done = sync_lead_tasks(s, lead, today)
            if done:
                lead_note += f" Auto-completed: {', '.join(done)}."

        # Task-linked draft: sending the reply completes the task.
        if draft.related_task_id:
            from app.models import Task, TaskActivity, TaskStatus

            task = s.get(Task, draft.related_task_id)
            if task is not None and task.status != TaskStatus.DONE:
                task.status = TaskStatus.DONE
                task.completed_at = func.now()
                s.add(
                    TaskActivity(
                        task_id=task.id, type="status_change",
                        detail="auto: reply sent from hub", actor="Mise",
                    )
                )
                lead_note += f" Task done: {task.title}."
    return f"Sent from {_mailbox_address(mailbox)}.{lead_note}"


def discard(draft_id: int) -> str:
    with db_session() as s:
        draft = s.get(EmailDraft, draft_id)
        if draft is None:
            return "Draft not found."
        draft.status = DraftStatus.DISCARDED
    return "Discarded."
