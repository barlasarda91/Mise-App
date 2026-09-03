"""APScheduler wiring (spec §10): cron per routine in LA time, persistent
jobstore in Postgres (schedules survive redeploys), plus manual "Run now".

Runs single-instance and in-process with the web service. If the app is ever
scaled beyond one replica, move the scheduler to a dedicated worker to avoid
duplicate firings (spec §3).
"""

import logging
import uuid

from apscheduler.jobstores.base import JobLookupError
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.db import db_session
from app.settings import get_settings

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def run_routine_job(routine_id: int, trigger: str = "scheduled") -> None:
    """Module-level entry point (persistent jobstores reference it by path).
    Executes in the scheduler's worker thread — never in an HTTP request."""
    from app.engine.runner import execute_run
    from app.models import RunTrigger

    execute_run(routine_id, RunTrigger(trigger))


def start_scheduler() -> BackgroundScheduler | None:
    """Start the scheduler with a Postgres-backed jobstore; returns None (and
    logs) when DATABASE_URL isn't configured."""
    global _scheduler
    url = get_settings().sqlalchemy_url
    if not url:
        log.warning("scheduler not started: DATABASE_URL is not configured")
        return None
    _scheduler = BackgroundScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=url, tablename="apscheduler_jobs")},
        timezone=get_settings().default_tz,
        job_defaults={
            "coalesce": True,  # a pile of missed firings collapses into one
            "misfire_grace_time": 3600,
            "max_instances": 1,  # never overlap runs of the same routine
        },
    )
    _scheduler.start()
    sync_jobs(_scheduler)
    log.info("scheduler started with %d job(s)", len(_scheduler.get_jobs()))
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def get_scheduler() -> BackgroundScheduler | None:
    return _scheduler


def sync_jobs(sched: BackgroundScheduler, session_factory=db_session) -> None:
    """Reconcile scheduler jobs with the routines table: enabled routines with
    a cron get a job (replacing any stale stored one); others are removed."""
    from app.models import Routine

    with session_factory() as s:
        routines = s.scalars(select(Routine)).all()
    for routine in routines:
        job_id = f"routine:{routine.key}"
        if routine.enabled and routine.schedule_cron:
            sched.add_job(
                run_routine_job,
                CronTrigger.from_crontab(routine.schedule_cron, timezone=routine.timezone),
                args=[routine.id, "scheduled"],
                id=job_id,
                name=routine.name,
                replace_existing=True,
            )
        else:
            try:
                sched.remove_job(job_id)
            except JobLookupError:
                pass


def trigger_run_now(routine_id: int, sched: BackgroundScheduler | None = None) -> bool:
    """Queue an immediate one-off manual run in the scheduler's thread pool.
    Returns False when the scheduler isn't running (no DB)."""
    sched = sched or _scheduler
    if sched is None:
        return False
    sched.add_job(
        run_routine_job,
        args=[routine_id, "manual"],
        id=f"manual:{routine_id}:{uuid.uuid4().hex[:8]}",
        name=f"manual run of routine {routine_id}",
    )
    return True
