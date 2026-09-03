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
