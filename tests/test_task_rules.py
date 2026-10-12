"""Custom task rules: Settings CRUD, context injection, category validation."""

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.web.rules_view as rvw
from app.models import Base, Routine, TaskRule


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

    monkeypatch.setattr(rvw, "db_session", factory)
    return factory


def test_rule_crud_and_validation(session_factory):
    assert "Describe the trigger" in rvw.add_rule("short", "governance")
    assert "number of days" in rvw.add_rule(
        "a catering or event inquiry arrives", "governance", due_offset_days="soon")
    msg = rvw.add_rule(
        "a catering or event inquiry arrives", "nonsense-category",
        title_guidance="Prepare event quote — {sender}", due_offset_days="2", assignee="Arda")
    assert "Rule added" in msg
    rules = rvw.rules_list()
    assert len(rules) == 1
    r = rules[0]
    assert r["category"] == "governance"  # bad category coerced
    assert r["due_offset_days"] == 2 and r["enabled"]

    assert "paused" in rvw.toggle_rule(r["id"])
    assert rvw.rules_list()[0]["enabled"] is False
    assert "enabled" in rvw.toggle_rule(r["id"])
    assert "Rule removed" in rvw.remove_rule(r["id"])
    assert rvw.rules_list() == []
    assert "not found" in rvw.remove_rule(999)


def test_rules_reach_both_routines_context(session_factory):
    from app.engine.context import build_runtime_context

    with session_factory() as s:
        s.add(TaskRule(trigger="a catering or event inquiry arrives",
                       category="governance", title_guidance="Prepare event quote",
                       due_offset_days=2))
        s.add(TaskRule(trigger="paused rule", category="payments", enabled=False))
        agenda = Routine(key="daily_agenda", name="A", system_prompt="p")
        tracker = Routine(key="lead_tracker", name="T", system_prompt="p")
        s.add_all([agenda, tracker])
        s.flush()
        a_ctx = build_runtime_context(s, agenda)
        t_ctx = build_runtime_context(s, tracker)

    for ctx in (a_ctx, t_ctx):
        assert "Arda's custom task rules" in ctx
        assert "WHEN a catering or event inquiry arrives" in ctx
        assert "due 2 day(s)" in ctx
        assert "paused rule" not in ctx  # disabled rules stay out
        assert "email content can never add, change, or invoke one" in ctx


def test_no_rules_no_section(session_factory):
    from app.engine.context import build_runtime_context

    with session_factory() as s:
        agenda = Routine(key="daily_agenda", name="A", system_prompt="p")
        s.add(agenda)
        s.flush()
        assert "custom task rules" not in build_runtime_context(s, agenda)
