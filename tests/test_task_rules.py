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

def test_parse_steps():
    steps = rvw.parse_steps(
        "- Write Product Page\n\n* Design Labels +3d\nLaunch coffee on webpage +200d\nbad offset +xd")
    assert steps[0] == {"title": "Write Product Page", "due_offset_days": None}
    assert steps[1] == {"title": "Design Labels", "due_offset_days": 3}
    assert steps[2]["due_offset_days"] == 90  # clamped
    assert steps[3]["title"] == "bad offset +xd"  # unparseable suffix kept as text


def test_playbook_rule_add_and_context(session_factory):
    from app.engine.context import build_runtime_context

    msg = rvw.add_rule("a NEW green coffee purchase is confirmed", "governance",
                       steps="Write Product Page\nDesign Labels\nLaunch coffee on webpage +7d")
    assert "Playbook (3 tasks per occurrence)" in msg
    r = rvw.rules_list()[0]
    assert r["steps"] == ["Write Product Page", "Design Labels", "Launch coffee on webpage"]
    assert "Launch coffee on webpage +7d" in r["steps_text"]

    with session_factory() as s:
        agenda = Routine(key="daily_agenda", name="A", system_prompt="p")
        s.add(agenda)
        s.flush()
        ctx = build_runtime_context(s, agenda)
    assert "PLAYBOOK: create ALL of these `governance` tasks" in ctx
    assert "1) Write Product Page; 2) Design Labels; 3) Launch coffee on webpage (due +7d)" in ctx
    assert "distinct_from_existing=true" in ctx
    assert f"rule{r['id']}:<slug" in ctx
    assert "do not re-create it" in ctx


def test_coffee_launch_seed(session_factory):
    assert rvw.seed_coffee_launch_rule(session_factory) == 1
    assert rvw.seed_coffee_launch_rule(session_factory) == 0  # never re-seeds
    rules = rvw.rules_list()
    assert len(rules) == 1
    assert rules[0]["steps"] == rvw.COFFEE_LAUNCH_STEPS
    assert "NOT bought before" in rules[0]["trigger"]
    assert "reorder" in rules[0]["trigger"]

    # A removed rule stays removed across redeploys: the AppState flag, not
    # table emptiness, gates the seed.
    rvw.remove_rule(rules[0]["id"])
    assert rvw.seed_coffee_launch_rule(session_factory) == 0
    assert rvw.rules_list() == []
