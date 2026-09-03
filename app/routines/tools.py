"""Model-facing tools for the routines (lead tracker; agenda reuses most).

Importing this module registers everything into the engine toolkit. All
mutations follow the review resolutions: hold-and-confirm for overdue timer
resets, gmail_msg_id idempotency, dedup ledger for tasks. Nothing here can
send email.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.engine.toolkit import ToolDef, get_run_context, register
from app.models import (
    ActivitySource,
    ExternalMutation,
    Lead,
    LeadActivity,
    LeadActivityType,
    LeadStage,
    MutationKind,
    OPEN_LEAD_STAGES,
    SyncSource,
    SyncState,
    Task,
    TaskCategory,
    TaskSource,
    TaskStatus,
)
from app.models.enums import FromMailbox
from app.routines.cadence import is_overdue
from app.settings import get_settings

NULLABLE_STR = {"type": ["string", "null"]}


def nullable_enum(values: list[str]) -> dict:
    # A nullable enum must be anyOf-composed: "enum" alongside a type union
    # (["string","null"]) is rejected by the API's strict schema validator.
    return {"anyOf": [{"type": "string", "enum": values}, {"type": "null"}]}


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().default_tz)


# ---------- Gmail (read-only + metadata; drafts arrive in milestone 9) ----------


def _search_gmail(session: Session, mailbox: str, query: str, after_date, max_results: int):
    from app.tools import gmail

    after = None
    if after_date:
        after = datetime.combine(date.fromisoformat(after_date), time.min, tzinfo=_tz())
    return gmail.search_messages(FromMailbox(mailbox), query, after, min(max_results, 50))


register(
    ToolDef(
        name="search_gmail",
        description=(
            "Search a Boxx mailbox with a Gmail query (e.g. 'in:sent to:x@y.com', "
            "'in:inbox wholesale'). after_date (YYYY-MM-DD, or null) bounds the search — "
            "derive it from the runtime context's last gather time, or the 90-day cold-start "
            "window on a first run. Returns header summaries (id, thread_id, from, to, "
            "subject, date, snippet), newest first."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "mailbox": {"type": "string", "enum": ["arda", "hello"]},
                "query": {"type": "string"},
                "after_date": NULLABLE_STR,
                "max_results": {"type": "integer"},
            },
            "required": ["mailbox", "query", "after_date", "max_results"],
            "additionalProperties": False,
        },
        handler=_search_gmail,
    )
)


def _get_gmail_message(session: Session, mailbox: str, msg_id: str):
    from app.tools import gmail

    return gmail.get_message(FromMailbox(mailbox), msg_id)


register(
    ToolDef(
        name="get_gmail_message",
        description="Fetch one Gmail message's headers and plain-text body by id.",
        input_schema={
            "type": "object",
            "properties": {
                "mailbox": {"type": "string", "enum": ["arda", "hello"]},
                "msg_id": {"type": "string"},
            },
            "required": ["mailbox", "msg_id"],
            "additionalProperties": False,
        },
        handler=_get_gmail_message,
    )
)


# ---------- lead mutations ----------


def _record_email_activity(
    session: Session, lead_id: int, gmail_msg_id: str, occurred_on: str, detail: str
):
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise ValueError(f"lead {lead_id} not found")
    if session.scalar(select(LeadActivity).where(LeadActivity.gmail_msg_id == gmail_msg_id)):
        return {"outcome": "already_recorded", "gmail_msg_id": gmail_msg_id}

    when = date.fromisoformat(occurred_on)
    today = datetime.now(_tz()).date()
    ctx = get_run_context()

    advances = lead.last_confirmed_action is None or when > lead.last_confirmed_action
    if advances and is_overdue(lead, today):
        # Hold-and-confirm: the found email would clear an overdue alert —
        # park it for Arda's one-click confirm in Pipeline (spec §7.1).
        lead.pending_confirmation = {
            "type": "email_sent",
            "occurred_on": occurred_on,
            "detail": detail,
            "gmail_msg_id": gmail_msg_id,
            "found_by_run_id": ctx.get("run_id"),
        }
        return {"outcome": "pending_confirmation", "lead": lead.business_name}

    session.add(
        LeadActivity(
            lead_id=lead_id,
            type=LeadActivityType.EMAIL_SENT,
            occurred_on=when,
            detail=detail,
            source=ActivitySource.GMAIL,
            gmail_msg_id=gmail_msg_id,
            run_id=ctx.get("run_id"),
        )
    )
    if advances:
        lead.last_confirmed_action = when
    return {"outcome": "recorded", "lead": lead.business_name, "advanced_timer": advances}


register(
    ToolDef(
        name="record_email_activity",
        description=(
            "Record a sent email found in the audit against a lead. Idempotent by "
            "gmail_msg_id. Advances the idle timer automatically UNLESS the lead is "
            "currently overdue — then it becomes a pending confirmation Arda approves "
            "in Pipeline before the timer resets. occurred_on = the email's actual date."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "lead_id": {"type": "integer"},
                "gmail_msg_id": {"type": "string"},
                "occurred_on": {"type": "string", "description": "YYYY-MM-DD"},
                "detail": {"type": "string", "description": "One line, e.g. subject line."},
            },
            "required": ["lead_id", "gmail_msg_id", "occurred_on", "detail"],
            "additionalProperties": False,
        },
        handler=_record_email_activity,
    )
)


def _create_lead(
    session: Session,
    business_name: str,
    contact_name,
    contact_email,
    contact_phone,
    lead_source,
    notes,
    gmail_msg_id,
):
    open_leads = session.scalars(select(Lead).where(Lead.stage.in_(OPEN_LEAD_STAGES))).all()
    for existing in open_leads:
        same_email = (
            contact_email
            and existing.contact_email
            and existing.contact_email.lower() == contact_email.lower()
        )
        if same_email or existing.business_name.lower() == business_name.lower():
            return {"outcome": "duplicate", "existing_lead_id": existing.id}

    today = datetime.now(_tz()).date()
    lead = Lead(
        business_name=business_name,
        contact_name=contact_name,
        contact_email=contact_email,
        contact_phone=contact_phone,
        lead_source=lead_source,
        stage=LeadStage.NEW,
        stage_since=today,
    )
    session.add(lead)
    session.flush()
    if notes:
        session.add(
            LeadActivity(
                lead_id=lead.id,
                type=LeadActivityType.NOTE,
                occurred_on=today,
                detail=notes,
                source=ActivitySource.GMAIL if gmail_msg_id else ActivitySource.MANUAL,
                run_id=get_run_context().get("run_id"),
            )
        )
    task = _create_task_impl(
        session,
        category="wholesale_leads",
        title=f"Qualify {business_name}",
        dedup_key=f"task:qualify:{gmail_msg_id or business_name.lower()}",
        description=notes,
        due_date=None,
        assignee=None,
        source_ref={"lead_id": lead.id, "gmail_msg_id": gmail_msg_id},
    )
    return {"outcome": "created", "lead_id": lead.id, "qualify_task": task}


register(
    ToolDef(
        name="create_lead",
        description=(
            "Insert a new wholesale lead at stage New (checks open leads for duplicates by "
            "email/name first) and create its 'Qualify …' board task. Use for inbound "
            "inquiries found in the inbox scan and cold-start reconstruction."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "business_name": {"type": "string"},
                "contact_name": NULLABLE_STR,
                "contact_email": NULLABLE_STR,
                "contact_phone": NULLABLE_STR,
                "lead_source": {"type": "string", "description": "e.g. inbound_email, referral, walk-in"},
                "notes": NULLABLE_STR,
                "gmail_msg_id": NULLABLE_STR,
            },
            "required": [
                "business_name",
                "contact_name",
                "contact_email",
                "contact_phone",
                "lead_source",
                "notes",
                "gmail_msg_id",
            ],
            "additionalProperties": False,
        },
        handler=_create_lead,
    )
)


def _update_lead(session: Session, lead_id: int, stage, loss_reason, note):
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise ValueError(f"lead {lead_id} not found")
    today = datetime.now(_tz()).date()
    changes = []
    if stage and stage != lead.stage.value:
        new_stage = LeadStage(stage)
        if new_stage == LeadStage.CLOSED_LOST and not loss_reason:
            raise ValueError("closing as lost requires loss_reason")
        session.add(
            LeadActivity(
                lead_id=lead_id,
                type=LeadActivityType.STAGE_CHANGE,
                occurred_on=today,
                detail=f"{lead.stage.value} → {new_stage.value}",
                run_id=get_run_context().get("run_id"),
            )
        )
        lead.stage = new_stage
        lead.stage_since = today
        if loss_reason:
            lead.loss_reason = loss_reason
        changes.append(f"stage → {new_stage.value}")
    if note:
        session.add(
            LeadActivity(
                lead_id=lead_id,
                type=LeadActivityType.NOTE,
                occurred_on=today,
                detail=note,
                run_id=get_run_context().get("run_id"),
            )
        )
        changes.append("note added")
    return {"outcome": "updated", "lead": lead.business_name, "changes": changes}


register(
    ToolDef(
        name="update_lead",
        description=(
            "Change a lead's stage and/or attach a note. Stage changes log a stage_change "
            "activity and reset stage_since; closing as closed_lost requires loss_reason. "
            "Does NOT touch the idle timer — that's record_email_activity / Arda's confirms."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "lead_id": {"type": "integer"},
                "stage": nullable_enum([s.value for s in LeadStage]),
                "loss_reason": NULLABLE_STR,
                "note": NULLABLE_STR,
            },
            "required": ["lead_id", "stage", "loss_reason", "note"],
            "additionalProperties": False,
        },
        handler=_update_lead,
    )
)


# ---------- tasks (deduped via the external_mutations ledger) ----------


def _create_task_impl(session, category, title, dedup_key, description, due_date, assignee, source_ref):
    ledger = session.scalar(select(ExternalMutation).where(ExternalMutation.dedup_key == dedup_key))
    due = date.fromisoformat(due_date) if due_date else None
    if ledger and ledger.external_id:
        task = session.get(Task, int(ledger.external_id))
        if task is not None:
            if due and task.due_date != due:
                task.due_date = due
            return {"outcome": "already_exists", "task_id": task.id, "title": task.title}
    task = Task(
        category=TaskCategory(category),
        title=title,
        description=description,
        due_date=due,
        assignee=assignee,
        source=TaskSource.ROUTINE,
        source_ref=source_ref,
    )
    session.add(task)
    session.flush()
    if ledger is None:
        session.add(
            ExternalMutation(
                kind=MutationKind.TASK,
                dedup_key=dedup_key,
                external_id=str(task.id),
                run_id=get_run_context().get("run_id"),
            )
        )
    else:
        ledger.external_id = str(task.id)
    return {"outcome": "created", "task_id": task.id, "title": title}


def _create_task(session: Session, category, title, dedup_key, description, due_date, assignee, lead_id):
    source_ref = {"lead_id": lead_id} if lead_id else None
    return _create_task_impl(session, category, title, dedup_key, description, due_date, assignee, source_ref)


register(
    ToolDef(
        name="create_task",
        description=(
            "Create a board task in one of the five categories, deduped by dedup_key — "
            "re-running with the same key updates instead of duplicating. Use stable keys: "
            "'followup:<lead_id>' for overdue-lead follow-ups, 'task:<gmail_msg_id>' for "
            "email-derived items, 'invoice:<qbo_invoice_id>' for A/R."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": [c.value for c in TaskCategory]},
                "title": {"type": "string"},
                "dedup_key": {"type": "string"},
                "description": NULLABLE_STR,
                "due_date": {"type": ["string", "null"], "description": "YYYY-MM-DD or null"},
                "assignee": NULLABLE_STR,
                "lead_id": {"type": ["integer", "null"]},
            },
            "required": ["category", "title", "dedup_key", "description", "due_date", "assignee", "lead_id"],
            "additionalProperties": False,
        },
        handler=_create_task,
    )
)


def _complete_task(session: Session, task_id: int):
    task = session.get(Task, task_id)
    if task is None:
        raise ValueError(f"task {task_id} not found")
    task.status = TaskStatus.DONE
    task.completed_at = func.now()
    return {"outcome": "completed", "task_id": task_id, "title": task.title}


register(
    ToolDef(
        name="complete_task",
        description="Mark a board task done (e.g. a follow-up that the sent-email audit shows happened).",
        input_schema={
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
        handler=_complete_task,
    )
)


def _update_task(session: Session, task_id: int, status, waiting_on, due_date, assignee):
    task = session.get(Task, task_id)
    if task is None:
        raise ValueError(f"task {task_id} not found")
    changes = []
    if status and status != task.status.value:
        task.status = TaskStatus(status)
        if task.status == TaskStatus.DONE:
            task.completed_at = func.now()
        changes.append(f"status → {status}")
    if waiting_on is not None:
        task.waiting_on = waiting_on or None
        changes.append("waiting_on set")
    if due_date is not None:
        task.due_date = date.fromisoformat(due_date) if due_date else None
        changes.append(f"due → {due_date or 'none'}")
    if assignee is not None:
        task.assignee = assignee or None
        changes.append(f"assignee → {assignee}")
    return {"outcome": "updated", "task_id": task_id, "changes": changes}


register(
    ToolDef(
        name="update_task",
        description=(
            "Update a board task: move status (todo/doing/waiting/done — waiting for "
            "blocked-on-third-party, with waiting_on saying who/what), set due date or "
            "assignee. Pass null for fields you're not changing."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "status": nullable_enum([s.value for s in TaskStatus]),
                "waiting_on": NULLABLE_STR,
                "due_date": {"type": ["string", "null"], "description": "YYYY-MM-DD, '' to clear, null to keep"},
                "assignee": NULLABLE_STR,
            },
            "required": ["task_id", "status", "waiting_on", "due_date", "assignee"],
            "additionalProperties": False,
        },
        handler=_update_task,
    )
)


# ---------- calendar + QuickBooks (read) ----------


def _list_calendar_events(session: Session, date_from: str, date_to: str):
    from app.tools import calendar

    tz = _tz()
    start = datetime.combine(date.fromisoformat(date_from), time.min, tzinfo=tz)
    end = datetime.combine(date.fromisoformat(date_to), time.max, tzinfo=tz)
    events = calendar.list_events(start, end)
    out = []
    for event in events:
        start_info = event.get("start") or {}
        out.append(
            {
                "summary": event.get("summary", "(no title)"),
                "start": start_info.get("dateTime") or start_info.get("date"),
                "end": (event.get("end") or {}).get("dateTime") or (event.get("end") or {}).get("date"),
                "all_day": "date" in start_info,
                "timezone_label": start_info.get("timeZone"),
                "location": event.get("location"),
                "attendees": [a.get("email") for a in event.get("attendees") or []],
            }
        )
    return out


register(
    ToolDef(
        name="list_calendar_events",
        description=(
            "List arda's calendar events between two dates inclusive (YYYY-MM-DD). "
            "Includes each event's timezone label so displayed-vs-actual offsets can be checked."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
            },
            "required": ["date_from", "date_to"],
            "additionalProperties": False,
        },
        handler=_list_calendar_events,
    )
)


def _list_overdue_invoices(session: Session):
    from app.tools import quickbooks

    today = datetime.now(_tz()).date()
    return quickbooks.list_overdue_invoices(today)


register(
    ToolDef(
        name="list_overdue_invoices",
        description=(
            "QuickBooks A/R: list overdue customer invoices (receivables Boxx has issued "
            "that are past due with a balance) — invoice id, doc number, customer, amount, "
            "balance, due date, days overdue. A/R only; A/P is out of scope."
        ),
        input_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        handler=_list_overdue_invoices,
    )
)


# ---------- email drafts (never sent — reviewed in the Drafts UI) ----------


def _create_email_draft(
    session: Session, mailbox: str, to, cc, subject, body, purpose, lead_id, task_id, gmail_thread_id
):
    from app.models import DraftStatus, EmailDraft
    from app.models.enums import FromMailbox as FM

    ctx = get_run_context()
    dedup_key = f"draft:{purpose}:{lead_id or task_id or 'none'}:{ctx.get('run_id')}"
    ledger = session.scalar(select(ExternalMutation).where(ExternalMutation.dedup_key == dedup_key))
    if ledger and ledger.external_id:
        return {"outcome": "already_exists", "draft_id": int(ledger.external_id)}
    draft = EmailDraft(
        subject=subject,
        body=body,
        to_addrs=to or None,
        cc_addrs=cc or None,
        from_mailbox=FM(mailbox),
        gmail_thread_id=gmail_thread_id,
        related_lead_id=lead_id,
        related_task_id=task_id,
        status=DraftStatus.COMPOSED,
        run_id=ctx.get("run_id"),
    )
    session.add(draft)
    session.flush()
    session.add(
        ExternalMutation(
            kind=MutationKind.GMAIL_DRAFT,
            dedup_key=dedup_key,
            external_id=str(draft.id),
            run_id=ctx.get("run_id"),
        )
    )
    return {"outcome": "created", "draft_id": draft.id}


register(
    ToolDef(
        name="create_email_draft",
        description=(
            "Prepare an email draft for Arda's review in the Drafts queue — it is NEVER "
            "sent automatically. Write the full subject and plain-text body yourself, "
            "matching the mailbox voice: arda = personal operator voice (greet 'Hey "
            "<first name>,', sign off exactly 'Best\\nArda', short and warm, concrete "
            "terms); hello = brand front-desk voice (self-introduce 'This is Arda from "
            "Boxx Coffee Roasters', brand 'we', service-first). Set gmail_thread_id when "
            "replying to an existing conversation. purpose = a short slug like "
            "'followup' or 'inquiry_reply' (used for dedup)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "mailbox": {"type": "string", "enum": ["arda", "hello"]},
                "to": {"type": "array", "items": {"type": "string"}},
                "cc": {"type": ["array", "null"], "items": {"type": "string"}},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "purpose": {"type": "string"},
                "lead_id": {"type": ["integer", "null"]},
                "task_id": {"type": ["integer", "null"]},
                "gmail_thread_id": NULLABLE_STR,
            },
            "required": ["mailbox", "to", "cc", "subject", "body", "purpose", "lead_id", "task_id", "gmail_thread_id"],
            "additionalProperties": False,
        },
        handler=_create_email_draft,
    )
)


# ---------- sync cursor ----------


def _mark_gather_complete(session: Session, source: str):
    ctx = get_run_context()
    routine_id = ctx.get("routine_id")
    started_at = ctx.get("started_at")
    if routine_id is None or started_at is None:
        raise RuntimeError("no run context — cannot mark gather complete")
    src = SyncSource(source)
    row = session.scalar(
        select(SyncState).where(SyncState.routine_id == routine_id, SyncState.source == src)
    )
    if row is None:
        row = SyncState(routine_id=routine_id, source=src)
        session.add(row)
    # Anchor to run start, not "now": mail arriving mid-run stays in the next window.
    row.last_run_at = started_at
    return {"outcome": "ok", "source": source, "gathered_through": str(started_at)}


register(
    ToolDef(
        name="mark_gather_complete",
        description=(
            "Call ONLY after successfully finishing the scan of a source this run "
            "(gmail_arda / gmail_hello / calendar). Advances that source's incremental "
            "cursor to this run's start time. Never call for a source whose scan failed "
            "or was skipped — the next run must re-cover that window."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": [s.value for s in SyncSource]},
            },
            "required": ["source"],
            "additionalProperties": False,
        },
        handler=_mark_gather_complete,
    )
)
