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
