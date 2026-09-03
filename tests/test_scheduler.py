from contextlib import contextmanager

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Routine
from app.routines.seed import ROUTINE_DEFAULTS, seed_routines
from app.scheduler import run_routine_job, sync_jobs, trigger_run_now


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def factory():
        session = maker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return factory


@pytest.fixture
def sched():
    scheduler = BackgroundScheduler(timezone="America/Los_Angeles")
    scheduler.start(paused=True)  # in-memory jobstore, nothing fires
    yield scheduler
    scheduler.shutdown(wait=False)


def test_seed_routines_idempotent(session_factory):
    assert seed_routines(session_factory) == len(ROUTINE_DEFAULTS)
    assert seed_routines(session_factory) == 0  # second call adds nothing
    with session_factory() as s:
        routines = s.query(Routine).all()
        assert {r.key for r in routines} == {"lead_tracker", "daily_agenda"}
        assert all(not r.enabled for r in routines)  # seeded off
        assert all(r.model == "opus" for r in routines)


def test_sync_jobs_adds_enabled_and_removes_disabled(session_factory, sched):
    seed_routines(session_factory)
    with session_factory() as s:
        tracker = s.query(Routine).filter_by(key="lead_tracker").one()
        tracker.enabled = True
        tracker_id = tracker.id

    sync_jobs(sched, session_factory)
    jobs = {j.id: j for j in sched.get_jobs()}
    assert set(jobs) == {"routine:lead_tracker"}
    assert jobs["routine:lead_tracker"].args == (tracker_id, "scheduled")
    # cron 30 8 * * * in LA
    trigger = jobs["routine:lead_tracker"].trigger
    assert "hour='8'" in str(trigger) and "minute='30'" in str(trigger)

    with session_factory() as s:
        s.query(Routine).filter_by(key="lead_tracker").one().enabled = False
    sync_jobs(sched, session_factory)
    assert sched.get_jobs() == []


def test_trigger_run_now_queues_manual_job(session_factory, sched):
    seed_routines(session_factory)
    assert trigger_run_now(7, sched) is True
    jobs = sched.get_jobs()
    assert len(jobs) == 1
    assert jobs[0].args == (7, "manual")
    assert jobs[0].id.startswith("manual:7:")


def test_trigger_run_now_without_scheduler():
    assert trigger_run_now(1, None) is False


def test_run_routine_job_invokes_engine(monkeypatch):
    calls = []
    import app.engine.runner as runner

    monkeypatch.setattr(runner, "execute_run", lambda rid, trig: calls.append((rid, trig.value)))
    run_routine_job(5, "manual")
    assert calls == [(5, "manual")]
