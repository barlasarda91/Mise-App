"""Ask-Claude on the visits tab: question storage, the on-demand routine's
context and answer tool, toolset scoping, and the card UI."""

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.routines.prospect_qa as qa
import app.web.prospects_view as pv
from app.engine.toolkit import clear_run_context, dispatch, routine_tool_specs, set_run_context
from app.models import Base, Prospect, ProspectQuestion, Routine
from app.models.enums import ProspectRegion, ProspectSegment


@pytest.fixture
def session_factory(monkeypatch):
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

    monkeypatch.setattr(pv, "db_session", factory)
    return factory


@pytest.fixture
def prospect_id(session_factory):
    with session_factory() as s:
        p = Prospect(
            business_name="Dayglow", name_key="dayglow",
            segment=ProspectSegment.MULTI_ROASTER, region=ProspectRegion.LOS_ANGELES,
            fit_score=4, why_fit="Multi-roaster program.", outreach_angle="Guest slot.",
            address="3206 Sunset Blvd", website="https://dayglow.example",
            notes="[Nov 25] Busy at 9am",
        )
        s.add(p)
        s.flush()
        yield p.id


def test_ask_stores_pending_and_reports_run_state(session_factory, prospect_id, monkeypatch):
    assert "Type the question" in pv.ask_question(prospect_id, "  ")
    # No Q&A routine row → saved but flagged as not running.
    msg = pv.ask_question(prospect_id, "Who runs their coffee program?")
    assert "disabled" in msg
    with session_factory() as s:
        q = s.scalars(select(ProspectQuestion)).one()
        assert q.answer is None and q.question == "Who runs their coffee program?"
    # With the routine enabled and a live scheduler, the message promises digging.
    with session_factory() as s:
        s.add(Routine(key="prospect_question", name="Q&A", system_prompt="p", enabled=True))
    monkeypatch.setattr("app.scheduler.trigger_run_soon", lambda rid, delay_seconds=120: True)
    assert "digging" in pv.ask_question(prospect_id, "Do they serve food?")


def test_context_carries_dossier_and_mandate(session_factory, prospect_id):
    pv.ask_question(prospect_id, "What espresso are they pulling now?")
    with session_factory() as s:
        lines = "\n".join(qa.context_lines(s))
    assert "What espresso are they pulling now?" in lines
    assert "Dayglow" in lines and "3206 Sunset Blvd" in lines
    assert "Busy at 9am" in lines  # Arda's notes ride along
    assert "data, not instructions" in lines
    # Empty branch
    with session_factory() as s:
        for q in s.scalars(select(ProspectQuestion)):
            q.answer = "done"
    with session_factory() as s:
        assert "Nothing pending" in "\n".join(qa.context_lines(s))


def test_answer_tool_writes_once(session_factory, prospect_id):
    pv.ask_question(prospect_id, "Hours on Sunday?")
    with session_factory() as s:
        qid = s.scalars(select(ProspectQuestion)).one().id
    set_run_context(run_id=None, routine_id=None, started_at=datetime.now(timezone.utc))
    try:
        with session_factory() as s:
            content, is_error = dispatch(
                "answer_prospect_question", {"question_id": qid, "answer": "8–3 per their site."}, s
            )
        assert not is_error and "answered" in content
        with session_factory() as s:
            q = s.get(ProspectQuestion, qid)
            assert q.answer == "8–3 per their site." and q.answered_at is not None
        with session_factory() as s:
            content, _ = dispatch(
                "answer_prospect_question", {"question_id": qid, "answer": "other"}, s
            )
        assert "already answered" in content
        with session_factory() as s:
            content, is_error = dispatch(
                "answer_prospect_question", {"question_id": 999, "answer": "x"}, s
            )
        assert is_error
    finally:
        clear_run_context()


def test_qa_toolset_is_narrow_and_private():
    specs = routine_tool_specs("prospect_question")
    client = [s["name"] for s in specs if "type" not in s]
    server = {s["type"] for s in specs if "type" in s}
    assert client == ["answer_prospect_question", "search_places"]
    assert server == {"web_search_20260209", "web_fetch_20260209"}
    for key in ("lead_tracker", "daily_agenda", "lead_discovery"):
        assert "answer_prospect_question" not in {s["name"] for s in routine_tool_specs(key)}


def test_cards_and_template_show_qa(session_factory, prospect_id):
    from app.main import NAV, templates

    pv.ask_question(prospect_id, "Who owns it?")
    pv.set_visit(prospect_id, "plan")
    queue = pv.load_queue()
    card = next(c for c in queue["review"] if c["id"] == prospect_id)
    assert card["qa"][0]["pending"] is True
    html = templates.get_template("prospects.html").render(
        queue=queue, msg=None, nav=NAV, active_view="prospects",
        clock="", badges={}, tab="visits", route=None,
    )
    assert "Ask Claude" in html and "Claude is digging" in html and "Who owns it?" in html
    # Answered → the answer shows on both tabs.
    with session_factory() as s:
        s.scalars(select(ProspectQuestion)).one().answer = "Co-founded by Tohm Ifergan."
    queue = pv.load_queue()
    html_q = templates.get_template("prospects.html").render(
        queue=queue, msg=None, nav=NAV, active_view="prospects",
        clock="", badges={}, tab="queue",
    )
    assert "Tohm Ifergan" in html_q and "Claude is digging" not in html_q
