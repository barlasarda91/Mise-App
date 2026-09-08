from datetime import datetime, timezone
from types import SimpleNamespace

from app.models import MessageRole
from app.web.runs_view import build_transcript, run_code


def _msg(role, content, when="2026-09-03T15:30:00+00:00"):
    return SimpleNamespace(
        role=role, content=content, created_at=datetime.fromisoformat(when)
    )


def test_run_code_prefixes():
    assert run_code("lead_tracker", 41) == "R-041"
    assert run_code("daily_agenda", 118) == "A-118"
    assert run_code("custom_thing", 7) == "C-007"


def test_build_transcript_shapes_blocks():
    messages = [
        _msg(MessageRole.USER, {"text": "## Runtime context\nWED 03 SEP"}),
        _msg(
            MessageRole.ASSISTANT,
            [
                {"type": "thinking", "thinking": ""},  # omitted display -> skipped
                {"type": "text", "text": "Checking the pipeline."},
                {"type": "tool_use", "id": "tu_1", "name": "list_open_leads", "input": {}},
            ],
        ),
        _msg(
            MessageRole.TOOL,
            [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "[]"},
                {"type": "tool_result", "tool_use_id": "tu_2", "content": "boom", "is_error": True},
            ],
        ),
    ]

    transcript = build_transcript(messages)
    assert [e["role"] for e in transcript] == ["user", "assistant", "tool"]

    user, assistant, tool = transcript
    assert "Runtime context" in user["blocks"][0]["text"]
    kinds = [b["kind"] for b in assistant["blocks"]]
    assert kinds == ["text", "tool_use"]  # empty thinking block dropped
    assert assistant["blocks"][1]["name"] == "list_open_leads"
    assert tool["blocks"][0]["is_error"] is False
    assert tool["blocks"][1]["is_error"] is True


def test_build_transcript_truncates_huge_tool_results():
    huge = "x" * 10_000
    messages = [_msg(MessageRole.TOOL, [{"type": "tool_result", "tool_use_id": "t", "content": huge}])]
    block = build_transcript(messages)[0]["blocks"][0]
    assert len(block["content"]) < 4000
    assert block["content"].endswith("(truncated)")


def test_times_rendered_in_la(monkeypatch):
    messages = [_msg(MessageRole.USER, {"text": "hi"}, "2026-09-03T15:30:00+00:00")]
    entry = build_transcript(messages)[0]
    assert entry["time"] == "03 SEP 08:30"  # UTC 15:30 -> LA 08:30 (PDT)


def test_final_report_takes_last_assistant_text():
    from app.web.runs_view import final_report

    messages = [
        _msg(MessageRole.ASSISTANT, [{"type": "text", "text": "Checking mail."}]),
        _msg(MessageRole.TOOL, [{"type": "tool_result", "content": "[]"}]),
        _msg(MessageRole.ASSISTANT, [
            {"type": "tool_use", "id": "t", "name": "list_tasks", "input": {}},
        ]),
        _msg(MessageRole.ASSISTANT, [{"type": "text", "text": "## Briefing\n- All clear."}]),
    ]
    assert final_report(messages) == "## Briefing\n- All clear."
    assert final_report([]) == ""


def test_load_todays_briefing_picks_first_run_plus_latest(monkeypatch):
    from contextlib import contextmanager
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.web.runs_view as rv
    from app.models import Base, Routine, Run, RunMessage
    from app.models.enums import RunStatus, RunTrigger
    from app.settings import get_settings

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def factory():
        s = maker()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    monkeypatch.setattr(rv, "db_session", factory)
    tz = ZoneInfo(get_settings().default_tz)
    now = datetime.now(tz)

    with factory() as s:
        routine = Routine(key="daily_agenda", name="Daily Agenda", system_prompt="p")
        s.add(routine)
        s.flush()

        def add_run(started, report):
            run = Run(routine_id=routine.id, status=RunStatus.COMPLETED,
                      trigger=RunTrigger.SCHEDULED, started_at=started)
            s.add(run)
            s.flush()
            s.add(RunMessage(run_id=run.id, role=MessageRole.ASSISTANT,
                             content=[{"type": "text", "text": report}]))
            return run.id

        add_run(now - timedelta(days=1), "Yesterday's briefing")  # excluded
        first_id = add_run(now.replace(hour=7, minute=2), "## Morning briefing\n- Vendors 10:30")
        latest_id = add_run(now.replace(hour=11, minute=0), "No changes since 10:00.")

    briefing = rv.load_todays_briefing()
    assert briefing["run_id"] == first_id
    assert "Morning briefing" in briefing["html"]
    assert briefing["latest"]["run_id"] == latest_id
    assert "No changes" in briefing["latest"]["html"]
