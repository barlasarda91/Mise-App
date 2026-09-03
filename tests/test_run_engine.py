from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.engine.runner import execute_run, sweep_orphan_runs
from app.engine.toolkit import dispatch, tool_specs
from app.models import (
    Base,
    Lead,
    LeadStage,
    MessageRole,
    Routine,
    Run,
    RunMessage,
    RunStatus,
    RunTrigger,
    Task,
    TaskCategory,
)


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
def routine_id(session_factory):
    with session_factory() as s:
        routine = Routine(key="lead_tracker", name="Lead Tracker", system_prompt="You are the tracker.")
        s.add(routine)
        s.add(
            Lead(
                business_name="Ember Room",
                stage=LeadStage.SAMPLED,
                last_confirmed_action=date(2026, 8, 19),
            )
        )
        s.add(Task(category=TaskCategory.GOVERNANCE, title="File quarterly sales tax"))
        s.flush()
        return routine.id


# ---------- fake Anthropic client ----------


class Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self):
        return dict(self.__dict__)


def text_block(text):
    return Block(type="text", text=text)


def tool_use_block(id, name, input):
    return Block(type="tool_use", id=id, name=name, input=input)


class FakeResponse:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


class FakeClient:
    """Scripted client.beta.messages.create; records the requests it saw."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []
        outer = self

        class _Messages:
            def create(self, **kwargs):
                # snapshot: the runner mutates its messages list after the call
                outer.requests.append({**kwargs, "messages": list(kwargs["messages"])})
                item = outer._responses.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        class _Beta:
            messages = _Messages()

        self.beta = _Beta()


def test_run_with_tool_use_completes_and_persists(session_factory, routine_id):
    client = FakeClient(
        [
            FakeResponse(
                [
                    text_block("Checking the pipeline."),
                    tool_use_block("tu_1", "list_open_leads", {}),
                ],
                "tool_use",
            ),
            FakeResponse([text_block("1 open lead; Ember Room is idle.")], "end_turn"),
        ]
    )

    run_id = execute_run(routine_id, RunTrigger.MANUAL, client=client, session_factory=session_factory)

    with session_factory() as s:
        run = s.get(Run, run_id)
        assert run.status == RunStatus.COMPLETED
        assert run.completed_at is not None
        assert run.trigger == RunTrigger.MANUAL

        msgs = s.query(RunMessage).filter_by(run_id=run_id).order_by(RunMessage.id).all()
        roles = [m.role for m in msgs]
        assert roles == [MessageRole.USER, MessageRole.ASSISTANT, MessageRole.TOOL, MessageRole.ASSISTANT]
        # runtime context injected as first user message
        assert "Ember Room" in msgs[0].content["text"]
        assert "File quarterly sales tax" in msgs[0].content["text"]
        # first-ever run: cold start scans ~3 months of backlog
        assert "past 90 days" in msgs[0].content["text"]
        # tool call captured on the assistant message
        assert msgs[1].tool_calls == [{"id": "tu_1", "name": "list_open_leads", "input": {}}]
        # tool result carried real data from the DB
        assert "Ember Room" in msgs[2].content[0]["content"]
        assert msgs[2].content[0]["tool_use_id"] == "tu_1"

    # request shape: cached system prompt, sorted tools, adaptive thinking
    first = client.requests[0]
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first["thinking"] == {"type": "adaptive"}
    assert [t["name"] for t in first["tools"]] == sorted(t["name"] for t in first["tools"])
    assert first["model"] == "claude-opus-5"  # 'opus' alias resolved
    assert first["fallbacks"] == "default"
    # second request carries the tool results back in one user message
    assert client.requests[1]["messages"][-1]["role"] == "user"


def test_api_failure_marks_run_failed(session_factory, routine_id):
    client = FakeClient([RuntimeError("api down")])
    run_id = execute_run(routine_id, client=client, session_factory=session_factory)
    with session_factory() as s:
        run = s.get(Run, run_id)
        assert run.status == RunStatus.FAILED
        assert "api down" in run.error


def test_refusal_marks_run_failed(session_factory, routine_id):
    client = FakeClient([FakeResponse([text_block("")], "refusal")])
    run_id = execute_run(routine_id, client=client, session_factory=session_factory)
    with session_factory() as s:
        assert s.get(Run, run_id).status == RunStatus.FAILED


def test_iteration_limit(session_factory, routine_id):
    endless = FakeResponse([tool_use_block("tu_x", "list_open_leads", {})], "tool_use")
    client = FakeClient([endless] * 40)
    run_id = execute_run(routine_id, client=client, session_factory=session_factory)
    with session_factory() as s:
        run = s.get(Run, run_id)
        assert run.status == RunStatus.FAILED
        assert "iteration limit" in run.error


def test_sweep_orphan_runs(session_factory, routine_id):
    with session_factory() as s:
        s.add(Run(routine_id=routine_id, status=RunStatus.RUNNING))
        s.add(Run(routine_id=routine_id, status=RunStatus.COMPLETED))
    assert sweep_orphan_runs(session_factory) == 1
    with session_factory() as s:
        failed = s.query(Run).filter_by(status=RunStatus.FAILED).all()
        assert len(failed) == 1
        assert "orphaned" in failed[0].error


def test_dispatch_unknown_tool_is_error(session_factory):
    with session_factory() as s:
        content, is_error = dispatch("nope", {}, s)
    assert is_error and "unknown tool" in content


def test_tool_specs_are_strict_and_sorted():
    specs = tool_specs()
    assert [t["name"] for t in specs] == sorted(t["name"] for t in specs)
    for spec in specs:
        assert spec["strict"] is True
        assert spec["input_schema"]["additionalProperties"] is False
