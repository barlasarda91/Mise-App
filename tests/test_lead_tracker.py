from contextlib import contextmanager
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.routines.tools  # noqa: F401 - registers tools
from app.engine.toolkit import clear_run_context, dispatch, set_run_context
from app.models import (
    Base,
    ExternalMutation,
    Lead,
    LeadActivity,
    LeadStage,
    Routine,
    SyncState,
    Task,
    TaskStatus,
)
from app.routines.cadence import is_overdue, overdue_by
from app.routines.seed import seed_routines


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


@pytest.fixture(autouse=True)
def run_context():
    set_run_context(run_id=None, routine_id=1, started_at=datetime(2026, 9, 3, 15, 30, tzinfo=timezone.utc))
    yield
    clear_run_context()


def _lead(stage, last_action=None, **kw):
    return Lead(business_name=kw.pop("name", "Ember Room"), stage=stage, last_confirmed_action=last_action, **kw)


# ---------- cadence ----------


def test_cadence_thresholds():
    today = date(2026, 9, 3)
    assert is_overdue(_lead(LeadStage.CONTACTED, date(2026, 8, 31)), today)  # 3d
    assert not is_overdue(_lead(LeadStage.CONTACTED, date(2026, 9, 1)), today)  # 2d
    assert is_overdue(_lead(LeadStage.SAMPLED, date(2026, 8, 28)), today)  # 6d
    assert not is_overdue(_lead(LeadStage.NEGOTIATING, date(2026, 8, 28)), today)  # 6d < 7
    assert is_overdue(_lead(LeadStage.NEGOTIATING, date(2026, 8, 27)), today)  # 7d
    assert not is_overdue(_lead(LeadStage.CLOSED_WON, date(2026, 1, 1)), today)
    assert overdue_by(_lead(LeadStage.SAMPLED, date(2026, 8, 28)), today) == 4  # 6 - 3 + 1


def test_cadence_falls_back_to_stage_since():
    today = date(2026, 9, 3)
    lead = _lead(LeadStage.NEW, None, stage_since=date(2026, 9, 2))
    assert is_overdue(lead, today)  # New alerts after a day
    fresh = _lead(LeadStage.NEW, None, stage_since=today)
    assert not is_overdue(fresh, today)


# ---------- routine tools ----------


def _call(session_factory, name, **input):
    import json

    with session_factory() as s:
        content, is_error = dispatch(name, input, s)
    return json.loads(content) if not is_error else {"error": content}


def test_create_lead_with_qualify_task_and_dedup(session_factory):
    result = _call(
        session_factory,
        "create_lead",
        business_name="Halcyon Coffee Bar",
        contact_name="Sam",
        contact_email="sam@halcyon.la",
        contact_phone=None,
        lead_source="inbound_email",
        notes="20-seat bar, wants pricing + samples",
        gmail_msg_id="m-100",
    )
    assert result["outcome"] == "created"
    assert result["qualify_task"]["title"] == "Qualify Halcyon Coffee Bar"

    dup = _call(
        session_factory,
        "create_lead",
        business_name="HALCYON coffee bar",
        contact_name=None,
        contact_email="other@x.com",
        contact_phone=None,
        lead_source="inbound_email",
        notes=None,
        gmail_msg_id="m-101",
    )
    assert dup["outcome"] == "duplicate"
    with session_factory() as s:
        assert s.query(Lead).count() == 1
        assert s.query(Task).count() == 1


def test_record_email_activity_advances_when_not_overdue(session_factory):
    with session_factory() as s:
        s.add(_lead(LeadStage.CONTACTED, date(2026, 9, 2)))  # 1d idle: not overdue
        s.flush()
        lead_id = s.query(Lead).one().id
    result = _call(
        session_factory,
        "record_email_activity",
        lead_id=lead_id,
        gmail_msg_id="m-1",
        occurred_on="2026-09-03",
        detail="Samples on the way",
    )
    assert result["outcome"] == "recorded" and result["advanced_timer"]
    # idempotent on re-scan
    again = _call(
        session_factory,
        "record_email_activity",
        lead_id=lead_id,
        gmail_msg_id="m-1",
        occurred_on="2026-09-03",
        detail="Samples on the way",
    )
    assert again["outcome"] == "already_recorded"
    with session_factory() as s:
        assert s.query(Lead).one().last_confirmed_action == date(2026, 9, 3)
        assert s.query(LeadActivity).count() == 1


def test_record_email_activity_holds_for_overdue_lead(session_factory):
    with session_factory() as s:
        s.add(_lead(LeadStage.SAMPLED, date(2026, 8, 19)))  # very overdue
        s.flush()
        lead_id = s.query(Lead).one().id
    result = _call(
        session_factory,
        "record_email_activity",
        lead_id=lead_id,
        gmail_msg_id="m-2",
        occurred_on="2026-09-01",
        detail="Checking in",
    )
    assert result["outcome"] == "pending_confirmation"
    with session_factory() as s:
        lead = s.query(Lead).one()
        assert lead.last_confirmed_action == date(2026, 8, 19)  # NOT advanced
        assert lead.pending_confirmation["gmail_msg_id"] == "m-2"
        assert s.query(LeadActivity).count() == 0  # activity lands on confirm


def test_update_lead_lost_requires_reason(session_factory):
    with session_factory() as s:
        s.add(_lead(LeadStage.NEGOTIATING, date(2026, 9, 1)))
        s.flush()
        lead_id = s.query(Lead).one().id
    err = _call(session_factory, "update_lead", lead_id=lead_id, stage="closed_lost", loss_reason=None, note=None)
    assert "loss_reason" in err["error"]
    ok = _call(session_factory, "update_lead", lead_id=lead_id, stage="closed_lost", loss_reason="price", note=None)
    assert ok["outcome"] == "updated"
    with session_factory() as s:
        lead = s.query(Lead).one()
        assert lead.stage == LeadStage.CLOSED_LOST and lead.loss_reason == "price"
        assert s.query(LeadActivity).filter_by(type="stage_change").count() == 1


def test_create_task_dedup_updates_not_duplicates(session_factory):
    first = _call(
        session_factory,
        "create_task",
        category="wholesale_leads",
        title="Follow up Ember Room — sampled, 6d idle",
        dedup_key="followup:1",
        description=None,
        due_date=None,
        assignee=None,
        lead_id=1,
    )
    assert first["outcome"] == "created"
    second = _call(
        session_factory,
        "create_task",
        category="wholesale_leads",
        title="Follow up Ember Room — sampled, 7d idle",
        dedup_key="followup:1",
        description=None,
        due_date="2026-09-05",
        assignee=None,
        lead_id=1,
    )
    assert second["outcome"] == "already_exists"
    with session_factory() as s:
        assert s.query(Task).count() == 1
        assert s.query(Task).one().due_date == date(2026, 9, 5)
        assert s.query(ExternalMutation).count() == 1


def test_mark_gather_complete_uses_run_start(session_factory):
    seed_routines(session_factory)
    with session_factory() as s:
        routine_id = s.query(Routine).filter_by(key="lead_tracker").one().id
    set_run_context(run_id=9, routine_id=routine_id, started_at=datetime(2026, 9, 3, 15, 30, tzinfo=timezone.utc))
    result = _call(session_factory, "mark_gather_complete", source="gmail_hello")
    assert result["outcome"] == "ok"
    with session_factory() as s:
        row = s.scalars(select(SyncState)).one()
        assert row.routine_id == routine_id
        assert row.last_run_at.replace(tzinfo=timezone.utc) == datetime(2026, 9, 3, 15, 30, tzinfo=timezone.utc)


# ---------- pipeline manual-entry services ----------


def test_pipeline_confirm_pending_applies(session_factory, monkeypatch):
    import app.web.pipeline_view as pv

    monkeypatch.setattr(pv, "db_session", session_factory)
    with session_factory() as s:
        lead = _lead(LeadStage.SAMPLED, date(2026, 8, 19))
        lead.pending_confirmation = {
            "type": "email_sent",
            "occurred_on": "2026-09-01",
            "detail": "Checking in",
            "gmail_msg_id": "m-2",
            "found_by_run_id": None,
        }
        s.add(lead)
        s.flush()
        lead_id = lead.id

    msg = pv.resolve_pending(lead_id, "confirm")
    assert "Confirmed" in msg
    with session_factory() as s:
        lead = s.query(Lead).one()
        assert lead.last_confirmed_action == date(2026, 9, 1)
        assert lead.pending_confirmation is None
        assert s.query(LeadActivity).one().gmail_msg_id == "m-2"


def test_pipeline_manual_call_advances_timer(session_factory, monkeypatch):
    import app.web.pipeline_view as pv

    monkeypatch.setattr(pv, "db_session", session_factory)
    with session_factory() as s:
        s.add(_lead(LeadStage.SAMPLED, date(2026, 8, 19)))
        s.flush()
        lead_id = s.query(Lead).one().id
    msg = pv.log_activity_manual(lead_id, "call", "2026-09-02", "wants 5 lb/wk")
    assert "Logged call" in msg
    with session_factory() as s:
        assert s.query(Lead).one().last_confirmed_action == date(2026, 9, 2)

    # notes don't touch the timer
    pv.log_activity_manual(lead_id, "note", "2026-09-03", "prefers filter")
    with session_factory() as s:
        assert s.query(Lead).one().last_confirmed_action == date(2026, 9, 2)
