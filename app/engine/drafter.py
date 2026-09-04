"""Model-assisted email drafting (spec §8.2).

Drafts follow the voice profile keyed to the sending mailbox (Voice A = arda@,
Voice B = hello@, from mise-voice-and-tone.md). Generation runs in a
background thread and updates the email_drafts row from DRAFTING to COMPOSED;
saving to Gmail is a separate, explicit step in the Drafts UI. Nothing here
can send email.
"""

import json
import logging
import threading
from pathlib import Path

from sqlalchemy import select

from app.db import db_session
from app.engine.anthropic_client import get_client, resolve_model
from app.models import DraftStatus, EmailDraft, Lead, LeadActivity
from app.models.enums import FromMailbox

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent / "routines" / "prompts"

DRAFT_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "to": {"type": "array", "items": {"type": "string"}},
            "cc": {"type": "array", "items": {"type": "string"}},
            "subject": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["to", "cc", "subject", "body"],
        "additionalProperties": False,
    },
}

DRAFTING_RULES = """
## Drafting rules
- Produce ONE email as JSON: to, cc, subject, body. Body is plain text (no markdown, no HTML).
- Use only facts from the context below and the instruction — never invent names, dates,
  prices, or commitments. If a detail is unknown, leave it out or keep it general.
- Only put addresses in "to"/"cc" that appear in the context; otherwise leave them empty
  for the operator to fill in.
- The draft will be reviewed and sent by Arda from Gmail — write it ready to send, no
  placeholders like [name] unless the name is genuinely unknown.
"""


def voice_profile(mailbox: FromMailbox) -> str:
    name = "voice_arda" if mailbox == FromMailbox.ARDA else "voice_hello"
    return (PROMPTS_DIR / f"{name}.md").read_text()


def build_system(mailbox: FromMailbox) -> str:
    return voice_profile(mailbox) + "\n" + DRAFTING_RULES


def lead_context(session, lead_id: int) -> str:
    lead = session.get(Lead, lead_id)
    if lead is None:
        return ""
    lines = [
        "## Lead context",
        f"Business: {lead.business_name}",
        f"Contact: {lead.contact_name or '?'} <{lead.contact_email or '?'}>"
        + (f" · {lead.contact_phone}" if lead.contact_phone else ""),
        f"Stage: {lead.stage.value} (since {lead.stage_since}) · last confirmed action {lead.last_confirmed_action or '—'}",
    ]
    activities = session.scalars(
        select(LeadActivity)
        .where(LeadActivity.lead_id == lead_id)
        .order_by(LeadActivity.occurred_on.desc(), LeadActivity.id.desc())
        .limit(8)
    ).all()
    if activities:
        lines.append("Recent activity:")
        lines += [f"- {a.occurred_on} · {a.type.value} · {a.detail or ''}" for a in activities]
    return "\n".join(lines)


def task_context(session, task_id: int) -> str:
    """Ground a task-linked draft in the task itself — for tasks with no lead
    or thread (e.g. routine-created governance items), this is often the only
    context the drafter gets."""
    from app.models import Task, TaskActivity

    task = session.get(Task, task_id)
    if task is None:
        return ""
    lines = [
        "## Board task this email is for",
        f"Task: {task.title}",
        f"Category: {task.category.value} · status {task.status.value} · due {task.due_date or '—'}",
    ]
    if task.description:
        lines.append(f"Details: {task.description}")
    if task.waiting_on:
        lines.append(f"Waiting on: {task.waiting_on}")
    activities = session.scalars(
        select(TaskActivity)
        .where(TaskActivity.task_id == task_id)
        .order_by(TaskActivity.id.desc())
        .limit(5)
    ).all()
    if activities:
        lines.append("Recent activity:")
        lines += [f"- {a.type} · {a.detail or ''}" for a in activities]
    return "\n".join(lines)


def thread_context(mailbox: FromMailbox, thread_id: str) -> str:
    try:
        from app.tools.gmail import get_thread_messages

        messages = get_thread_messages(mailbox, thread_id)
    except Exception as exc:
        return f"## Thread context\n(unavailable: {type(exc).__name__})"
    lines = ["## Thread being replied to (oldest of the recent messages first)"]
    for m in messages:
        lines.append(f"--- From: {m['from']} · {m['date']} · Subject: {m['subject']}")
        lines.append((m.get("body") or m.get("snippet") or "")[:2500])
    return "\n".join(lines)


def generate_content(client, mailbox: FromMailbox, instruction: str, context_text: str) -> dict:
    response = client.messages.create(
        model=resolve_model("opus"),
        max_tokens=2000,
        system=build_system(mailbox),
        output_config={"format": DRAFT_SCHEMA},
        messages=[
            {
                "role": "user",
                "content": f"{context_text}\n\n## Instruction\nDraft this email: {instruction}",
            }
        ],
    )
    text = next(b.text for b in response.content if getattr(b, "type", None) == "text")
    return json.loads(text)


def generate_draft_job(draft_id: int, instruction: str, client=None, session_factory=db_session) -> None:
    """Background worker: fill in a DRAFTING row and mark it COMPOSED."""
    client = client or get_client()
    with session_factory() as s:
        draft = s.get(EmailDraft, draft_id)
        if draft is None:
            return
        mailbox = draft.from_mailbox
        context_parts = []
        if draft.related_lead_id:
            context_parts.append(lead_context(s, draft.related_lead_id))
        if draft.related_task_id:
            context_parts.append(task_context(s, draft.related_task_id))
        thread_id = draft.gmail_thread_id

    if thread_id:
        context_parts.append(thread_context(mailbox, thread_id))
    context_text = "\n\n".join(p for p in context_parts if p) or "(no linked context)"

    try:
        content = generate_content(client, mailbox, instruction, context_text)
    except Exception as exc:
        log.exception("draft %s generation failed", draft_id)
        with session_factory() as s:
            draft = s.get(EmailDraft, draft_id)
            draft.body = f"(generation failed: {type(exc).__name__}: {exc} — edit by hand or discard)"
            draft.status = DraftStatus.COMPOSED
        return

    with session_factory() as s:
        draft = s.get(EmailDraft, draft_id)
        draft.subject = content.get("subject") or draft.subject
        draft.body = content.get("body") or ""
        # Operator- or lead-provided recipients always win; the model only
        # fills the gaps it found in context.
        if content.get("to") and not draft.to_addrs:
            draft.to_addrs = content["to"]
        if content.get("cc") and not draft.cc_addrs:
            draft.cc_addrs = content["cc"]
        draft.status = DraftStatus.COMPOSED


def spawn_draft_generation(draft_id: int, instruction: str) -> None:
    threading.Thread(
        target=generate_draft_job, args=(draft_id, instruction), daemon=True
    ).start()
