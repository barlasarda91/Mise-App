from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, Lead, LeadStage, Task, TaskCategory, TaskStatus
from app.routines.task_sync import sync_lead_tasks

TODAY = date(2026, 9, 3)


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


def _setup(session, stage, last_action=None, tasks=()):
    lead = Lead(business_name="Golden Nook", stage=stage, last_confirmed_action=last_action)
    session.add(lead)
    session.flush()
    for title in tasks:
        session.add(
            Task(category=TaskCategory.WHOLESALE_LEADS, title=title, source_ref={"lead_id": lead.id})
        )
    session.flush()
    return lead


def test_qualify_completes_when_lead_leaves_new(session_factory):
    with session_factory() as s:
        lead = _setup(s, LeadStage.CONTACTED, TODAY, ["Qualify Golden Nook"])
        done = sync_lead_tasks(s, lead, TODAY)
        assert done == ["Qualify Golden Nook"]
        assert s.query(Task).one().status == TaskStatus.DONE


def test_qualify_stays_open_while_new(session_factory):
    with session_factory() as s:
        lead = _setup(s, LeadStage.NEW, None, ["Qualify Golden Nook"])
        assert sync_lead_tasks(s, lead, TODAY) == []
        assert s.query(Task).one().status == TaskStatus.TODO


def test_followup_completes_when_no_longer_overdue(session_factory):
    with session_factory() as s:
        lead = _setup(s, LeadStage.CONTACTED, TODAY, ["Follow up Golden Nook — contacted, 20d idle"])
        done = sync_lead_tasks(s, lead, TODAY)
        assert done and s.query(Task).one().status == TaskStatus.DONE


def test_followup_stays_while_overdue(session_factory):
    with session_factory() as s:
        lead = _setup(s, LeadStage.CONTACTED, date(2026, 8, 14), ["Follow up Golden Nook"])
        assert sync_lead_tasks(s, lead, TODAY) == []


def test_closed_lead_completes_all_its_tasks(session_factory):
    with session_factory() as s:
        lead = _setup(
            s, LeadStage.CLOSED_LOST, date(2026, 8, 14),
            ["Qualify Golden Nook", "Follow up Golden Nook", "Price Golden Nook @ 8 lb/wk"],
        )
        done = sync_lead_tasks(s, lead, TODAY)
        assert len(done) == 3
        assert all(t.status == TaskStatus.DONE for t in s.query(Task).all())


def test_other_leads_tasks_untouched(session_factory):
    with session_factory() as s:
        lead = _setup(s, LeadStage.CONTACTED, TODAY, ["Qualify Golden Nook"])
        s.add(Task(category=TaskCategory.WHOLESALE_LEADS, title="Qualify Other Cafe", source_ref={"lead_id": 999}))
        s.flush()
        done = sync_lead_tasks(s, lead, TODAY)
        assert done == ["Qualify Golden Nook"]
        other = s.query(Task).filter_by(title="Qualify Other Cafe").one()
        assert other.status == TaskStatus.TODO


def test_task_email_context_via_source_message(monkeypatch):
    import app.tools.gmail as gm
    import app.web.board_view as bv
    import app.web.drafts_view as dv

    def fake_get_message(mailbox, msg_id):
        if mailbox.value == "arda":
            raise RuntimeError("not in this mailbox")
        return {"id": msg_id, "thread_id": "t-la"}

    monkeypatch.setattr(gm, "get_message", fake_get_message)
    monkeypatch.setattr(
        dv, "load_thread",
        lambda sel: {
            "error": None, "label": "thread", "thread_id": sel["thread_id"], "mailbox": sel["mailbox"],
            "messages": [
                {"from": "Arda <hello@boxxcoffee.com>", "date": "Mon", "subject": "", "body": "pricelist"},
                {"from": "Adam <adam@lacoffeeclub.com>", "date": "Tue", "subject": "", "body": "cutoff?"},
            ],
        },
    )
    ctx = bv.load_task_email_context({"lead_id": None, "gmail_msg_id": "m-la"})
    assert ctx["thread_id"] == "t-la"
    assert ctx["mailbox"] == "hello"
    assert ctx["reply_addr"] == "adam@lacoffeeclub.com"  # newest non-Boxx sender


def test_start_generation_links_task_and_prefills_to(session_factory, monkeypatch):
    import app.web.drafts_view as dv
    from app.models import EmailDraft

    import app.engine.drafter as drafter

    monkeypatch.setattr(dv, "db_session", session_factory)
    monkeypatch.setattr(drafter, "spawn_draft_generation", lambda *a: None)
    msg, draft_id = dv.start_generation(
        "reply with cutoff dates", "arda", "", "t-la", task_id="52", to="adam@lacoffeeclub.com"
    )
    assert draft_id
    with session_factory() as s:
        draft = s.get(EmailDraft, draft_id)
        assert draft.related_task_id == 52
        assert draft.to_addrs == ["adam@lacoffeeclub.com"]
        assert draft.gmail_thread_id == "t-la"
