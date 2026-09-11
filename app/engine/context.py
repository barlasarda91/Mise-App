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
MAX_TASKS_PER_CATEGORY = 12

# First-ever-run gather window: with no last_run_at for a source, routines
# scan the past 90 days (~3 months) of backlog instead of "everything".
COLD_START_DAYS = 90


def build_runtime_context(session, routine: Routine, trigger: str | None = None) -> str:
    tz = ZoneInfo(routine.timezone or get_settings().default_tz)
    now = datetime.now(tz)
    today = now.date()
    lines = [
        "## Runtime context",
        f"Current datetime: {now.strftime('%A %Y-%m-%d %H:%M')} ({tz})",
    ]
    if trigger == "manual":
        lines.append(
            "Trigger: MANUAL — Arda pressed Run now and wants a full fresh look, "
            "not just the hourly delta."
        )
    elif trigger:
        lines.append(f"Trigger: {trigger}")

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
        select(Lead)
        .where(Lead.stage.in_(OPEN_LEAD_STAGES), Lead.discarded_at.is_(None))
        .order_by(Lead.id)
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

    # General-mail triage is the agenda's job; the tracker gets its wholesale
    # picture from the lead list instead.
    if routine.key == "daily_agenda":
        try:
            from app.tools.mail_index import awaiting_reply

            waiting = awaiting_reply(session)
        except Exception:
            waiting = []
        if waiting:
            lines.append(
                "\n## Threads awaiting a Boxx reply (local 90-day mail index — read or "
                "unread; bulk mail filtered; only people Boxx has actually corresponded with)"
            )
            for t in waiting:
                lines.append(
                    f"- [{t['mailbox']}@ · msg {t['gmail_msg_id']}] {t['from_name']} — "
                    f"\"{t['subject']}\" · last message {t['last_message']} ({t['age_days']}d ago)"
                )
            lines.append(
                "Every thread above must be triaged — either it reaches the briefing with a "
                "task, or you explicitly judge it not-actionable in one line. Silently "
                "skipping an entry is not allowed. get_gmail_message (with the msg id) for detail."
            )

    from app.models import DisregardRule

    rules = session.scalars(select(DisregardRule).order_by(DisregardRule.id.desc()).limit(20)).all()
    if rules:
        lines.append("\n## Disregarded by Arda — never surface, never create tasks for these")
        for rule in rules:
            parts = [p for p in (rule.title, rule.contact_email) if p]
            lines.append(f"- {' · '.join(parts)}")
        lines.append("Mail from these senders / about these items is noise: skip it silently.")

    # Grouped by category with a per-category cap so a crowded tab (say, a
    # wave of invoice tasks) can never push another tab — governance reply
    # tasks especially — out of the model's view entirely.
    tasks = session.scalars(select(Task).where(Task.status != TaskStatus.DONE)).all()
    lines.append(
        f"\n## Incomplete board tasks ({len(tasks)}) — every category below is "
        "briefing material, not just the dated ones"
    )
    from app.models import TaskCategory

    by_category: dict = {}
    for task in tasks:
        by_category.setdefault(task.category, []).append(task)
    for category in TaskCategory:
        group = by_category.get(category)
        if not group:
            continue
        group.sort(key=lambda t: (t.due_date is None, t.due_date or today, t.id))
        lines.append(f"### {category.value} ({len(group)})")
        for task in group[:MAX_TASKS_PER_CATEGORY]:
            due = f" · due {task.due_date}" if task.due_date else ""
            overdue = " · OVERDUE" if task.due_date and task.due_date < today else ""
            age = ""
            if not task.due_date and task.created_at:
                created = task.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=ZoneInfo("UTC"))
                days_on_board = max(0, (today - created.astimezone(tz).date()).days)
                age = f" · on board {days_on_board}d"
            lines.append(f"- [{task.id}] {task.title} · {task.status.value}{due}{overdue}{age}")
        if len(group) > MAX_TASKS_PER_CATEGORY:
            lines.append(
                f"- …plus {len(group) - MAX_TASKS_PER_CATEGORY} more {category.value} "
                "tasks (list_tasks for the rest)"
            )

    return "\n".join(lines)
