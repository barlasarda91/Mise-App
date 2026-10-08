"""Keep the board honest about the pipeline: when a lead advances, its
routine-created tasks complete themselves.

Rules:
- lead left New            -> open "Qualify …" tasks for it complete
- lead no longer overdue   -> open "Follow up …" tasks for it complete
- lead closed (won/lost)   -> every open wholesale-leads task for it completes

Called wherever a lead advances: pipeline manual entry, hub Send, and the
run tools (update_lead / record_email_activity).
"""

from datetime import date

from sqlalchemy import func, select

from app.models import Lead, LeadStage, Task, TaskActivity, TaskCategory, TaskStatus
from app.routines.cadence import is_overdue

CLOSED = (LeadStage.CLOSED_WON, LeadStage.CLOSED_LOST)

MIN_MATCH_NAME_CHARS = 4  # names shorter than this match too loosely


def match_lead_for_task(session, title: str, description: str | None = None) -> Lead | None:
    """Confident lead match for a task: an open, non-discarded lead whose full
    business name appears in the task's title or description. Ambiguity (two
    candidates, neither name containing the other) returns None — no guessing."""
    from app.models import OPEN_LEAD_STAGES

    text = f"{title} {description or ''}".lower()
    leads = session.scalars(
        select(Lead).where(Lead.stage.in_(OPEN_LEAD_STAGES), Lead.discarded_at.is_(None))
    ).all()
    candidates = [
        l
        for l in leads
        if len(l.business_name.strip()) >= MIN_MATCH_NAME_CHARS
        and l.business_name.strip().lower() in text
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda l: len(l.business_name), reverse=True)
    if len(candidates) > 1:
        longest, runner_up = candidates[0], candidates[1]
        # "LA Coffee Club" vs "LA Coffee" is fine (longest wins); two unrelated
        # names both present is ambiguous.
        if runner_up.business_name.strip().lower() not in longest.business_name.strip().lower():
            return None
    return candidates[0]


def auto_link_lead(session, task: Task) -> Lead | None:
    """Attach a confident lead match to an unlinked task. Returns the lead."""
    if (task.source_ref or {}).get("lead_id"):
        return None
    lead = match_lead_for_task(session, task.title, task.description)
    if lead is None:
        return None
    ref = dict(task.source_ref or {})
    ref["lead_id"] = lead.id
    task.source_ref = ref  # reassign: JSON columns don't track in-place edits
    session.add(
        TaskActivity(
            task_id=task.id, type="linked",
            detail=f"auto: linked to lead {lead.business_name} (name match)", actor="Mise",
        )
    )
    return lead


def auto_link_open_tasks() -> int:
    """One-shot sweep: link existing open, unlinked tasks to leads by confident
    name match. Runs at startup; idempotent. Returns how many were linked."""
    from app.db import db_session

    linked = 0
    with db_session() as session:
        open_tasks = session.scalars(
            select(Task).where(Task.status != TaskStatus.DONE)
        ).all()
        for task in open_tasks:
            if auto_link_lead(session, task) is not None:
                linked += 1
    return linked


def sync_lead_tasks(session, lead: Lead, today: date) -> list[str]:
    """Auto-complete board tasks made moot by the lead's current state.
    Returns the completed task titles."""
    open_tasks = session.scalars(
        select(Task).where(
            Task.category == TaskCategory.WHOLESALE_LEADS,
            Task.status != TaskStatus.DONE,
        )
    ).all()
    completed = []
    for task in open_tasks:
        if (task.source_ref or {}).get("lead_id") != lead.id:
            continue
        title = task.title.lower()
        reason = None
        if lead.stage in CLOSED:
            reason = f"lead closed ({lead.stage.value})"
        elif title.startswith("qualify") and lead.stage != LeadStage.NEW:
            reason = f"lead advanced to {lead.stage.value}"
        elif title.startswith("follow up") and not is_overdue(lead, today):
            reason = "follow-up done — idle timer reset"
        if reason:
            task.status = TaskStatus.DONE
            task.completed_at = func.now()
            session.add(
                TaskActivity(task_id=task.id, type="status_change", detail=f"auto: {reason}", actor="Mise")
            )
            completed.append(task.title)
    return completed


# Money tasks: a reply on the source thread doesn't prove payment — these are
# never auto-completed, only surfaced to the agenda for judgment.
MONEY_CATEGORIES = (TaskCategory.PAYMENTS, TaskCategory.INVOICE_TRACKING)


def _replied_source_thread(session, task: Task):
    """The newest message on the task's source email thread, when that newest
    message is an outbound Boxx reply sent after the task was created —
    the strong signal the hanging item was handled. None otherwise."""
    from datetime import timezone

    from app.models import MailMessage

    ref = task.source_ref or {}
    msg_id = ref.get("gmail_msg_id")
    if not msg_id:
        return None
    src = session.scalar(select(MailMessage).where(MailMessage.gmail_msg_id == msg_id))
    if src is None or not src.thread_id:
        return None
    newest = session.scalar(
        select(MailMessage)
        .where(
            MailMessage.mailbox == src.mailbox,
            MailMessage.thread_id == src.thread_id,
            MailMessage.sent_at.isnot(None),
        )
        .order_by(MailMessage.sent_at.desc())
    )
    if newest is None or not newest.is_outbound:
        return None  # no reply, or the counterparty spoke last — still open
    created = task.created_at
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    sent = newest.sent_at if newest.sent_at.tzinfo else newest.sent_at.replace(tzinfo=timezone.utc)
    if created is not None and sent <= created:
        return None
    return newest


def auto_resolve_replied_tasks(session) -> list[str]:
    """Zero-token board hygiene, run before each executed run: an open
    email-derived task whose source thread's NEWEST message is a Boxx reply
    sent after the task existed gets completed automatically — Arda handled
    it from Gmail and the board shouldn't keep nagging. Money tasks
    (payments / invoice_tracking) are exempt: replying isn't paying."""
    from datetime import datetime, timezone

    resolved = []
    open_tasks = session.scalars(select(Task).where(Task.status != TaskStatus.DONE)).all()
    for task in open_tasks:
        if task.category in MONEY_CATEGORIES:
            continue
        reply = _replied_source_thread(session, task)
        if reply is None:
            continue
        task.status = TaskStatus.DONE
        task.completed_at = datetime.now(timezone.utc)
        when = reply.sent_at.date().isoformat() if reply.sent_at else "recently"
        session.add(
            TaskActivity(
                task_id=task.id,
                type="auto_resolved",
                detail=f"Boxx replied on the source thread ({when}) and nothing newer arrived — auto-completed",
                actor="Mise",
            )
        )
        resolved.append(f"[{task.id}] {task.title}")
    return resolved


def replied_money_tasks(session) -> list[Task]:
    """Open payments / invoice_tracking tasks whose source thread was replied
    to — candidates the agenda should verify and complete, never auto-done."""
    out = []
    for task in session.scalars(select(Task).where(Task.status != TaskStatus.DONE)):
        if task.category not in MONEY_CATEGORIES:
            continue
        if _replied_source_thread(session, task) is not None:
            out.append(task)
    return out
