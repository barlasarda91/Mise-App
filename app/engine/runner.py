"""Run engine: single-phase autonomous execution (spec §4).

A run is one autonomous agentic turn — system prompt + runtime context, then a
manual tool-use loop that persists every step to run_messages as it goes. A
manual loop (rather than the SDK tool runner) is deliberate: each iteration is
written to Postgres, tools dispatch through the registry with a fresh DB
session, and tests inject a fake client.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select, update

from app.db import db_session
from app.engine.anthropic_client import get_client, resolve_model
from app.engine.context import build_runtime_context
from app.engine.toolkit import dispatch, tool_specs
from app.models import MessageRole, Routine, Run, RunMessage, RunStatus, RunTrigger

log = logging.getLogger(__name__)

MAX_ITERATIONS = 30
MAX_TOKENS = 16000


def _serialize_blocks(content) -> list:
    out = []
    for block in content:
        out.append(block.model_dump() if hasattr(block, "model_dump") else dict(block))
    return out


def _persist_message(session_factory, run_id: int, role: MessageRole, content, tool_calls=None):
    with session_factory() as s:
        s.add(RunMessage(run_id=run_id, role=role, content=content, tool_calls=tool_calls))


def _set_run_status(session_factory, run_id: int, status: RunStatus, error: str | None = None):
    with session_factory() as s:
        run = s.get(Run, run_id)
        run.status = status
        run.error = error
        run.completed_at = datetime.now(timezone.utc)


def execute_run(
    routine_id: int,
    trigger: RunTrigger = RunTrigger.SCHEDULED,
    client=None,
    session_factory=db_session,
) -> int:
    """Execute one autonomous run of a routine. Returns the run id; the run
    ends `completed` or `failed` — errors are recorded, not raised."""
    client = client or get_client()

    with session_factory() as s:
        routine = s.get(Routine, routine_id)
        if routine is None:
            raise ValueError(f"routine {routine_id} not found")
        model = resolve_model(routine.model)
        system_prompt = routine.system_prompt
        context_text = build_runtime_context(s, routine)
        run = Run(routine_id=routine_id, trigger=trigger, status=RunStatus.RUNNING)
        s.add(run)
        s.flush()
        run_id = run.id

    messages: list[dict] = [{"role": "user", "content": context_text}]
    _persist_message(session_factory, run_id, MessageRole.USER, {"text": context_text})

    # Request shape: stable system prompt carries the cache breakpoint; the
    # volatile runtime context lives in messages, after it. Tool list is
    # sorted (toolkit) so the cached prefix stays byte-stable across runs.
    request_base = dict(
        model=model,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        thinking={"type": "adaptive"},
        tools=tool_specs(),
        # Server-side refusal-fallback routing (Claude API default for Opus 5):
        # a safety decline re-runs the request on a fallback model in-call.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )

    try:
        for _ in range(MAX_ITERATIONS):
            response = client.beta.messages.create(**request_base, messages=messages)

            tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            _persist_message(
                session_factory,
                run_id,
                MessageRole.ASSISTANT,
                _serialize_blocks(response.content),
                tool_calls=[{"id": b.id, "name": b.name, "input": b.input} for b in tool_uses] or None,
            )
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "pause_turn":
                continue

            if response.stop_reason == "tool_use":
                results = []
                with session_factory() as s:
                    for block in tool_uses:
                        content, is_error = dispatch(block.name, block.input, s)
                        result = {"type": "tool_result", "tool_use_id": block.id, "content": content}
                        if is_error:
                            result["is_error"] = True
                        results.append(result)
                messages.append({"role": "user", "content": results})
                _persist_message(session_factory, run_id, MessageRole.TOOL, results)
                continue

            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                _set_run_status(session_factory, run_id, RunStatus.FAILED, f"refusal ({category})")
                return run_id

            # end_turn, max_tokens, stop_sequence — the run is finished.
            if response.stop_reason == "max_tokens":
                log.warning("run %s hit max_tokens; transcript may be truncated", run_id)
            _set_run_status(session_factory, run_id, RunStatus.COMPLETED)
            return run_id

        _set_run_status(session_factory, run_id, RunStatus.FAILED, "iteration limit reached")
    except Exception as exc:
        log.exception("run %s failed", run_id)
        _set_run_status(session_factory, run_id, RunStatus.FAILED, f"{type(exc).__name__}: {exc}")
    return run_id


def sweep_orphan_runs(session_factory=db_session) -> int:
    """Mark runs stuck in `running` (orphaned by a redeploy/crash) as failed.
    Called on app startup (spec §4 resilience)."""
    with session_factory() as s:
        result = s.execute(
            update(Run)
            .where(Run.status == RunStatus.RUNNING)
            .values(
                status=RunStatus.FAILED,
                error="orphaned: app restarted mid-run",
                completed_at=func.now(),
            )
        )
        return result.rowcount or 0


def latest_runs(session, limit: int = 30) -> list[Run]:
    return list(session.scalars(select(Run).order_by(Run.started_at.desc(), Run.id.desc()).limit(limit)))
