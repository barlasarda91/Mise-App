"""Cost roadmap phase 0 (usage instrumentation) and 1a (skip-if-quiet)."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.engine.preflight as pf
from app.engine.pricing import usage_cost
from app.engine.runner import execute_run
from app.models import Base, Routine, Run, RunStatus, RunTrigger, SyncState
from app.models.enums import SyncSource

from tests.test_run_engine import Block, FakeClient, FakeResponse, text_block


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


def _routine(s, key="lead_tracker", connectors=("gmail_arda", "gmail_hello")):
    routine = Routine(key=key, name=key, system_prompt="p", connectors=list(connectors))
    s.add(routine)
    s.flush()
    return routine


def _usage(**kw):
    defaults = dict(input_tokens=0, output_tokens=0,
                    cache_read_input_tokens=0, cache_creation_input_tokens=0)
    defaults.update(kw)
    return Block(**defaults)


# ---------- phase 0: pricing + usage capture ----------


def test_usage_cost_math():
    u = _usage(input_tokens=100_000, output_tokens=10_000,
               cache_read_input_tokens=200_000, cache_creation_input_tokens=20_000)
    # opus: 0.1*5 + 0.2*0.5 + 0.02*6.25 + 0.01*25 = 0.5+0.1+0.125+0.25
    assert usage_cost("claude-opus-5", u) == pytest.approx(0.975)
    # sonnet at 2/10
    assert usage_cost("claude-sonnet-5", u) == pytest.approx(0.2 + 0.04 + 0.05 + 0.1)
    # unknown models price high (opus rates)
    assert usage_cost("mystery-model", u) == pytest.approx(0.975)


def test_run_accumulates_usage_and_cost(session_factory):
    with session_factory() as s:
        routine_id = _routine(s).id

    r1 = FakeResponse([text_block("checking")], "end_turn")
    r1.usage = _usage(input_tokens=10_000, output_tokens=1_000, cache_read_input_tokens=5_000)
    r1.model = "claude-opus-5"
    client = FakeClient([r1])
    run_id = execute_run(routine_id, trigger=RunTrigger.MANUAL,
                         client=client, session_factory=session_factory)

    with session_factory() as s:
        run = s.get(Run, run_id)
        assert run.status == RunStatus.COMPLETED
        assert run.usage["iterations"] == 1
        assert run.usage["input_tokens"] == 10_000
        assert run.usage["cache_read_input_tokens"] == 5_000
        # 0.01*5 + 0.005*0.5 + 0.001*25 = 0.05 + 0.0025 + 0.025
        assert float(run.cost_usd) == pytest.approx(0.0775, abs=1e-4)


# ---------- phase 1a: skip-if-quiet ----------


def _quiet_setup(s, key="lead_tracker"):
    """A routine with a completed run today and gather cursors an hour old."""
    routine = _routine(s, key=key)
    s.add(Run(routine_id=routine.id, status=RunStatus.COMPLETED,
              started_at=datetime.now(timezone.utc)))
    for source in (SyncSource.GMAIL_ARDA, SyncSource.GMAIL_HELLO):
        s.add(SyncState(routine_id=routine.id, source=source,
                        last_run_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    return routine


def test_manual_and_first_of_day_always_run(session_factory, monkeypatch):
    monkeypatch.setattr(pf, "_new_mail_count", lambda mb, since: 0)
    with session_factory() as s:
        routine = _routine(s)
        assert pf.skip_reason(s, routine, RunTrigger.MANUAL) is None
        # no completed run today -> first of day -> run
        assert pf.skip_reason(s, routine, RunTrigger.SCHEDULED) is None


def test_quiet_hour_skips_and_new_mail_runs(session_factory, monkeypatch):
    with session_factory() as s:
        routine = _quiet_setup(s)
        s.commit()

        monkeypatch.setattr(pf, "_new_mail_count", lambda mb, since: 0)
        reason = pf.skip_reason(s, routine, RunTrigger.SCHEDULED)
        assert reason and "no new mail" in reason

        monkeypatch.setattr(pf, "_new_mail_count", lambda mb, since: 2)
        assert pf.skip_reason(s, routine, RunTrigger.SCHEDULED) is None


def test_agenda_runs_when_calendar_changes(session_factory, monkeypatch):
    monkeypatch.setattr(pf, "_new_mail_count", lambda mb, since: 0)
    with session_factory() as s:
        routine = _quiet_setup(s, key="daily_agenda")
        s.commit()

        monkeypatch.setattr(pf, "_calendar_fingerprint", lambda: "aaa")
        # first comparison: nothing stored yet -> counts as changed -> run
        assert pf.skip_reason(s, routine, RunTrigger.SCHEDULED) is None
        s.commit()
        # unchanged now -> quiet
        reason = pf.skip_reason(s, routine, RunTrigger.SCHEDULED)
        assert reason and "calendar unchanged" in reason
        # event moved -> run
        monkeypatch.setattr(pf, "_calendar_fingerprint", lambda: "bbb")
        assert pf.skip_reason(s, routine, RunTrigger.SCHEDULED) is None


def test_preflight_failure_fails_open(session_factory, monkeypatch):
    def boom(mb, since):
        raise RuntimeError("gmail down")

    monkeypatch.setattr(pf, "_new_mail_count", boom)
    with session_factory() as s:
        routine = _quiet_setup(s)
        s.commit()
        assert pf.skip_reason(s, routine, RunTrigger.SCHEDULED) is None


def test_execute_run_records_skip_without_model_call(session_factory, monkeypatch):
    with session_factory() as s:
        routine_id = _quiet_setup(s).id

    monkeypatch.setattr(pf, "_new_mail_count", lambda mb, since: 0)
    client = FakeClient([])  # any create() call would pop from empty and raise
    run_id = execute_run(routine_id, trigger=RunTrigger.SCHEDULED,
                         client=client, session_factory=session_factory)

    assert client.requests == []
    with session_factory() as s:
        run = s.get(Run, run_id)
        assert run.status == RunStatus.SKIPPED
        assert run.cost_usd is None
        from app.models import RunMessage

        note = s.query(RunMessage).filter_by(run_id=run_id).one()
        assert "Skipped" in note.content[0]["text"]
