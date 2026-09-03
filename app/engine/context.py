"""Runtime context injected into every run (spec §10).

The model doesn't know the wall-clock date, what happened since the last run,
or the hub's state — this builds that as a compact text block. It is the first
user message and deliberately sits AFTER the cached system prompt, since it
changes every run.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models import Lead, OPEN_LEAD_STAGES, Routine, SyncState, Task, TaskStatus
from app.settings import get_settings

MAX_LEADS = 40
MAX_TASKS = 25

# First-ever-run gather window: with no last_run_at for a source, routines
# scan the past 90 days (~3 months) of backlog instead of "everything".
COLD_START_DAYS = 90


def build_runtime_context(session, routine: Routine) -> str:
    tz = ZoneInfo(routine.timezone or get_settings().default_tz)
    now = datetime.now(tz)
    today = now.date()
    lines = [
        "## Runtime context",
        f"Current datetime: {now.strftime('%A %Y-%m-%d %H:%M')} ({tz})",
    ]

    sync_rows = session.scalars(
        select(SyncState).where(SyncState.routine_id == routine.id).order_by(SyncState.source)
    ).all()
    if sync_rows and any(r.last_run_at for r in sync_rows):
        lines.append("Last successful gather per source:")
        for row in sync_rows:
            stamp = (
                row.last_run_at.astimezone(tz).strftime("%Y-%m-%d %H:%M")
                if row.last_run_at
                else f"never — scan the past {COLD_START_DAYS} days of backlog for this source"
            )
            lines.append(f"- {row.source.value}: {stamp}")
    else:
        lines.append(
            f"No sync state yet — this is this routine's FIRST EVER run (cold start): "
            f"scan the past {COLD_START_DAYS} days (~3 months) of backlog across your sources."
        )

    leads = session.scalars(
        select(Lead).where(Lead.stage.in_(OPEN_LEAD_STAGES)).order_by(Lead.id)
    ).all()
    lines.append(f"\n## Open leads ({len(leads)})")
    def idle(lead: Lead) -> int:
        return (today - lead.last_confirmed_action).days if lead.last_confirmed_action else -1
    for lead in sorted(leads, key=idle, reverse=True)[:MAX_LEADS]:
        idle_days = idle(lead)
        idle_txt = f"{idle_days}d idle" if idle_days >= 0 else "no confirmed action yet"
        pending = " · PENDING CONFIRMATION" if lead.pending_confirmation else ""
        lines.append(
            f"- [{lead.id}] {lead.business_name} · {lead.stage.value.upper()} · "
            f"last action {lead.last_confirmed_action or '—'} · {idle_txt}{pending}"
        )

    tasks = session.scalars(
        select(Task).where(Task.status != TaskStatus.DONE).order_by(Task.due_date.is_(None), Task.due_date)
    ).all()
    lines.append(f"\n## Incomplete board tasks ({len(tasks)})")
    for task in tasks[:MAX_TASKS]:
        due = f" · due {task.due_date}" if task.due_date else ""
        overdue = " · OVERDUE" if task.due_date and task.due_date < today else ""
        lines.append(
            f"- [{task.id}] {task.title} · {task.category.value} · {task.status.value}{due}{overdue}"
        )

    return "\n".join(lines)
