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
