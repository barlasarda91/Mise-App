"""Board page: five standing categories, kanban by status (spec §8.1)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db import db_session
from app.models import Task, TaskActivity, TaskCategory, TaskPriority, TaskSource, TaskStatus
from app.settings import get_settings

CATEGORIES = [
    (TaskCategory.WHOLESALE_LEADS, "Wholesale Leads"),
    (TaskCategory.CONSULTATION, "Consultation"),
    (TaskCategory.POP_UPS, "Pop-Ups"),
    (TaskCategory.INVOICE_TRACKING, "Invoice Tracking"),
    (TaskCategory.GOVERNANCE, "Governance"),
]

STATUSES = [
    (TaskStatus.TODO, "To do"),
    (TaskStatus.DOING, "Doing"),
    (TaskStatus.WAITING, "Waiting"),
    (TaskStatus.DONE, "Done"),
]


def _today() -> date:
    return datetime.now(ZoneInfo(get_settings().default_tz)).date()


def _source_line(task: Task) -> str:
    ref = task.source_ref or {}
    if ref.get("lead_id"):
        return f"lead #{ref['lead_id']}"
    if ref.get("qbo_invoice_id") or (task.source == TaskSource.QUICKBOOKS):
        return "QuickBooks"
    if ref.get("gmail_msg_id"):
        return "email"
    return task.source.value

def _card(task: Task, today: date) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "due": task.due_date,
        "overdue": bool(task.due_date and task.due_date < today and task.status != TaskStatus.DONE),
        "assignee": task.assignee,
        "priority": task.priority.value,
        "waiting_on": task.waiting_on,
        "source": _source_line(task),
        "status": task.status.value,
        "lead_id": (task.source_ref or {}).get("lead_id"),
    }


def load_boards() -> list[dict]:
    today = _today()
    try:
        with db_session() as s:
            tasks = s.scalars(select(Task).order_by(Task.due_date.is_(None), Task.due_date, Task.id)).all()
    except Exception:
        tasks = []
    boards = []
    for category, label in CATEGORIES:
        cat_tasks = [t for t in tasks if t.category == category]
        lanes = []
        for status, status_label in STATUSES:
            lane_tasks = [t for t in cat_tasks if t.status == status]
            if status == TaskStatus.DONE:
                lane_tasks = lane_tasks[-8:]  # recent done only; history stays in DB
            lanes.append(
                {
                    "status": status.value,
                    "label": status_label,
                    "cards": [_card(t, today) for t in lane_tasks],
                }
            )
        boards.append(
            {
                "key": category.value,
                "label": label,
                "lanes": lanes,
                "open": sum(1 for t in cat_tasks if t.status != TaskStatus.DONE),
            }
        )
    return boards


def load_task(task_id: int) -> dict | None:
    today = _today()
    try:
        with db_session() as s:
            task = s.get(Task, task_id)
            if task is None:
                return None
            ref = task.source_ref or {}
            links = []
            if ref.get("lead_id"):
                links.append({"label": "Open lead in Pipeline", "href": f"/pipeline/lead/{ref['lead_id']}", "external": False})
            if ref.get("gmail_msg_id"):
                links.append({
                    "label": "Open email in Gmail",
                    "href": f"https://mail.google.com/mail/u/0/#all/{ref['gmail_msg_id']}",
                    "external": True,
                })
            if ref.get("qbo_invoice_id"):
                links.append({
                    "label": "Open invoice in QuickBooks",
                    "href": f"https://app.qbo.intuit.com/app/invoice?txnId={ref['qbo_invoice_id']}",
                    "external": True,
                })
            from app.models import EmailDraft

            drafts = s.scalars(select(EmailDraft).where(EmailDraft.related_task_id == task_id)).all()
            for d in drafts:
                links.append({"label": f"Open draft: {d.subject or '(no subject)'}", "href": f"/drafts?draft={d.id}", "external": False})

            activities = s.scalars(
                select(TaskActivity).where(TaskActivity.task_id == task_id).order_by(TaskActivity.id.desc())
            ).all()
            return {
                **_card(task, today),
                "gmail_msg_id": ref.get("gmail_msg_id"),
                "description": task.description,
                "category": task.category.value,
                "category_label": dict((c.value, l) for c, l in CATEGORIES)[task.category.value],
                "created_at": task.created_at,
                "completed_at": task.completed_at,
                "links": links,
                "activities": [
                    {"type": a.type, "detail": a.detail, "actor": a.actor, "at": a.created_at}
                    for a in activities
                ],
            }
    except Exception:
        return None


def _counterpart_addr(messages: list[dict]) -> str:
    """Newest non-Boxx sender in a conversation — the address a reply goes to."""
    from email.utils import parseaddr

    for m in reversed(messages):
        addr = parseaddr(m.get("from", ""))[1]
        if addr and "boxxcoffee.com" not in addr.lower():
            return addr
    return ""


def load_task_email_context(task: dict | None) -> dict | None:
    """Email conversation behind a task: via its linked lead's contact, or —
    for email-sourced tasks with no lead — by locating the source message's
    thread in either mailbox."""
    if not task:
        return None
    if task.get("lead_id"):
        try:
            with db_session() as s:
                from app.models import Lead

                lead = s.get(Lead, task["lead_id"])
                email = lead.contact_email if lead else None
        except Exception:
            email = None
        if not email:
            return None
        from app.web.pipeline_view import load_email_context

        ctx = load_email_context({"contact_email": email})
        if ctx is not None:
            ctx["reply_addr"] = email
        return ctx

    msg_id = task.get("gmail_msg_id")
    if not msg_id:
        return None
    from app.models.enums import FromMailbox
    from app.tools import gmail
    from app.web.drafts_view import load_thread

    last_error = None
    for mailbox in ("arda", "hello"):
        try:
            message = gmail.get_message(FromMailbox(mailbox), msg_id)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        ctx = load_thread({"thread_id": message.get("thread_id"), "mailbox": mailbox, "to": ""})
        if ctx is not None:
            ctx["reply_addr"] = _counterpart_addr(ctx.get("messages") or [])
            return ctx
    if last_error:
        return {"error": last_error, "messages": [], "label": "thread", "thread_id": None,
                "mailbox": "arda", "reply_addr": ""}
    return None


# ---------- manual services ----------


def create_task_manual(category: str, title: str, due_date: str, assignee: str, priority: str) -> str:
    if not title.strip():
        return "Title is required."
    with db_session() as s:
        task = Task(
            category=TaskCategory(category),
            title=title.strip(),
            due_date=date.fromisoformat(due_date) if due_date else None,
            assignee=assignee.strip() or None,
            priority=TaskPriority(priority) if priority else TaskPriority.NORMAL,
            source=TaskSource.MANUAL,
        )
        s.add(task)
        s.flush()
        s.add(TaskActivity(task_id=task.id, type="created", detail=title.strip(), actor="Arda"))
    return f"Task added: {title.strip()}."


def edit_task_manual(task_id: int, due_date: str, assignee: str, priority: str, description: str) -> str:
    with db_session() as s:
        task = s.get(Task, task_id)
        if task is None:
            return "Task not found."
        task.due_date = date.fromisoformat(due_date) if due_date else None
        task.assignee = assignee.strip() or None
        task.priority = TaskPriority(priority)
        task.description = description.strip() or None
        s.add(TaskActivity(task_id=task_id, type="edited", detail="details updated", actor="Arda"))
    return "Task updated."


def set_task_status(task_id: int, status: str, waiting_on: str = "") -> str:
    new_status = TaskStatus(status)
    with db_session() as s:
        task = s.get(Task, task_id)
        if task is None:
            return "Task not found."
        if task.status == new_status:
            return "Status unchanged."
        s.add(
            TaskActivity(
                task_id=task_id,
                type="status_change",
                detail=f"{task.status.value} → {new_status.value}",
                actor="Arda",
            )
        )
        task.status = new_status
        task.completed_at = datetime.now(ZoneInfo(get_settings().default_tz)) if new_status == TaskStatus.DONE else None
        if new_status == TaskStatus.WAITING and waiting_on.strip():
            task.waiting_on = waiting_on.strip()
        if new_status != TaskStatus.WAITING:
            task.waiting_on = None
        title = task.title
    return f"{title} → {new_status.value}."
