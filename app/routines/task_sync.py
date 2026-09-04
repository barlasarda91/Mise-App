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
