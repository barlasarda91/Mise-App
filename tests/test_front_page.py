"""Front-page Today: standfirst extraction, Arda-promoted pins, promote route."""

from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.web.briefing_pins as bp
import app.web.runs_view as rv
from app.models import Base, Routine, Run, RunMessage, Task, TaskCategory, TaskStatus
from app.models.enums import MessageRole, RunStatus, RunTrigger
from app.settings import get_settings


@pytest.fixture
def session_factory(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def factory():
        s = maker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(rv, "db_session", factory)
    monkeypatch.setattr(bp, "db_session", factory)
    return factory


def _seed_run(s, report):
    routine = Routine(key="daily_agenda", name="Daily Agenda", system_prompt="p")
    s.add(routine)
    s.flush()
    now = datetime.now(ZoneInfo(get_settings().default_tz))
    run = Run(routine_id=routine.id, status=RunStatus.COMPLETED,
              trigger=RunTrigger.SCHEDULED, started_at=now.replace(hour=7, minute=2))
    s.add(run)
    s.flush()
    s.add(RunMessage(run_id=run.id, role=MessageRole.ASSISTANT,
                     content=[{"type": "text", "text": report}]))


def test_standfirst_lifted_out_as_headline(session_factory):
    with session_factory() as s:
        _seed_run(s, "STANDFIRST: Two meetings, five payment fronts open.\n"
                     "## Act today\n- [ ] Pay Aliso (#1)\n\n## Schedule\n- 10:30 Vendors")
        s.add(Task(id=1, category=TaskCategory.PAYMENTS, title="Pay Aliso"))

    briefing = rv.load_todays_briefing()
    assert briefing["standfirst"] == "Two meetings, five payment fronts open."
    assert "STANDFIRST" not in briefing["html"]
    assert [i["task_ids"] for i in briefing["checklist"]] == [[1]]


def test_pins_toggle_prune_and_merge_into_checklist(session_factory):
    with session_factory() as s:
        _seed_run(s, "STANDFIRST: Quiet day.\n## Act today\n- [ ] Pay Aliso (#1)\n\nBody — Gold Mountain naturals (#2).")
        s.add(Task(id=1, category=TaskCategory.PAYMENTS, title="Pay Aliso"))
        s.add(Task(id=2, category=TaskCategory.GOVERNANCE, title="Answer Gold Mountain"))
        s.add(Task(id=3, category=TaskCategory.GOVERNANCE, title="Old thing", status=TaskStatus.DONE))

    # pin a body-only task + toggle semantics
    assert bp.toggle_pin(2)["pinned"] is True
    assert bp.toggle_pin(999)["ok"] is False
    briefing = rv.load_todays_briefing()
    promoted = [i for i in briefing["checklist"] if i.get("promoted")]
    assert len(promoted) == 1 and promoted[0]["task_ids"] == [2]
    assert 2 in briefing["all_task_ids"]

    # pinning a task already in the checklist tags it, no duplicate row
    assert bp.toggle_pin(1)["pinned"] is True
    briefing = rv.load_todays_briefing()
    rows_for_1 = [i for i in briefing["checklist"] if i["task_ids"] == [1]]
    assert len(rows_for_1) == 1 and rows_for_1[0]["promoted"] is True

    # unpin removes; done tasks are pruned from the store automatically
    assert bp.toggle_pin(2)["pinned"] is False
    with session_factory() as s:
        bp._store(s, [1, 3])
    with session_factory() as s:
        kept = [t.id for t in bp.open_pins(s)]
    assert kept == [1]
    with session_factory() as s:
        assert bp.pinned_ids(s) == [1]


def test_promoted_pins_reach_runtime_context(session_factory):
    from app.engine.context import build_runtime_context

    with session_factory() as s:
        routine = Routine(key="daily_agenda", name="A", system_prompt="p")
        tracker = Routine(key="lead_tracker", name="T", system_prompt="p")
        s.add_all([routine, tracker])
        s.add(Task(id=5, category=TaskCategory.GOVERNANCE, title="Answer Gold Mountain"))
        s.flush()
        bp._store(s, [5])
        s.flush()
        agenda_ctx = build_runtime_context(s, routine)
        tracker_ctx = build_runtime_context(s, tracker)
    assert "Arda-promoted" in agenda_ctx and "[5] Answer Gold Mountain" in agenda_ctx
    assert "Arda-promoted" not in tracker_ctx


def test_run_health_flags_trailing_failures_only(session_factory):
    """Banner condition: an ENABLED routine whose latest executed run failed.
    A later success clears it; skipped runs don't count as executions;
    disabled routines never alarm."""
    from datetime import timezone as dt_tz

    from app.models.enums import RunStatus as RS

    now = datetime.now(dt_tz.utc)

    def run_at(s, routine, minutes_ago, status, error=None):
        r = Run(routine_id=routine.id, status=status, trigger=RunTrigger.SCHEDULED,
                started_at=now - timedelta(minutes=minutes_ago))
        r.error = error
        s.add(r)

    with session_factory() as s:
        agenda = Routine(key="daily_agenda", name="Daily Agenda", system_prompt="p", enabled=True)
        tracker = Routine(key="lead_tracker", name="Tracker", system_prompt="p", enabled=True)
        dead = Routine(key="old", name="Old", system_prompt="p", enabled=False)
        s.add_all([agenda, tracker, dead])
        s.flush()
        # agenda: ok, then three failures, newest skipped (ignored)
        run_at(s, agenda, 240, RS.COMPLETED)
        run_at(s, agenda, 180, RS.FAILED, "BadRequestError: credit balance too low")
        run_at(s, agenda, 120, RS.FAILED, "BadRequestError: credit balance too low")
        run_at(s, agenda, 60, RS.FAILED, "BadRequestError: credit balance too low")
        run_at(s, agenda, 30, RS.SKIPPED)
        # tracker: failed earlier but recovered -> healthy
        run_at(s, tracker, 120, RS.FAILED, "boom")
        run_at(s, tracker, 60, RS.COMPLETED)
        # disabled routine failing -> ignored
        run_at(s, dead, 60, RS.FAILED, "ignored")

    failing = rv.run_health()
    assert len(failing) == 1
    f = failing[0]
    assert f["routine"] == "Daily Agenda" and f["streak"] == 3
    assert "credit balance too low" in f["error"]

    # recovery clears the banner
    with session_factory() as s:
        routine = s.scalars(rv.select(Routine).filter_by(key="daily_agenda")).first()
        run_at(s, routine, 5, RS.COMPLETED)
    assert rv.run_health() == []
