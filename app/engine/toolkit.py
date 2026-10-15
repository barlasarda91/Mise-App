"""Model-facing tool registry for runs.

Each tool = JSON schema (strict) + a handler taking (session, **input) and
returning a JSON-serializable result. Routine milestones (7-8) register their
own tools; the core read tools below give every routine its state snapshot.

INVARIANT: nothing here (or registered later) may send email — drafts only.
"""

import json
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Lead, OPEN_LEAD_STAGES, Task, TaskStatus


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Any]  # handler(session, **tool_input)
    # Strict mode is off for all tools: the API's strict-schema compiler
    # rejects toolsets like ours ("too many union parameters", "schema is
    # too complex"). Handlers default their optional params and validate
    # inputs themselves; dispatch returns errors the model can correct.
    strict: bool = False
    # None = shared: offered to every routine without its own toolset.
    # A tuple scopes the tool to those routine keys only.
    routines: tuple[str, ...] | None = None


_REGISTRY: dict[str, ToolDef] = {}

# Routines that get an explicit, narrow toolset instead of the shared one
# (e.g. lead discovery never needs Gmail search or the task board).
ROUTINE_TOOLSETS: dict[str, list[str]] = {}

# Anthropic server-side tools (web search/fetch) per routine — executed by
# the API itself, so they never pass through dispatch().
SERVER_TOOLS: dict[str, list[dict]] = {}

# Ambient run context so handlers know which run/routine is executing
# (e.g. mark_gather_complete needs the run's start time and routine id).
_RUN_CONTEXT: ContextVar[dict | None] = ContextVar("run_context", default=None)


def set_run_context(**kwargs) -> None:
    _RUN_CONTEXT.set(kwargs)


def get_run_context() -> dict:
    return _RUN_CONTEXT.get() or {}


def clear_run_context() -> None:
    _RUN_CONTEXT.set(None)


def register(tool: ToolDef) -> ToolDef:
    if tool.name in _REGISTRY:
        raise ValueError(f"duplicate tool name: {tool.name}")
    _REGISTRY[tool.name] = tool
    return tool


def tool_specs(names: list[str] | None = None) -> list[dict]:
    """API tool definitions, sorted by name so the request prefix stays
    byte-stable for prompt caching."""
    tools = _REGISTRY.values() if names is None else [_REGISTRY[n] for n in names]
    out = []
    for t in sorted(tools, key=lambda t: t.name):
        spec = {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        if t.strict:
            spec["strict"] = True
        out.append(spec)
    return out


def routine_tool_specs(routine_key: str | None) -> list[dict]:
    """The tools a routine's runs are offered: its own toolset when it
    declares one, else every shared tool; server tools come last so the
    prefix stays byte-stable."""
    if routine_key in ROUTINE_TOOLSETS:
        specs = tool_specs(ROUTINE_TOOLSETS[routine_key])
    else:
        specs = tool_specs([t.name for t in _REGISTRY.values() if t.routines is None])
    return specs + [dict(t) for t in SERVER_TOOLS.get(routine_key, [])]


MAX_RESULT_CHARS = 24000  # safety net (cost roadmap 1c): no tool result floods the loop


def dispatch(name: str, tool_input: dict, session: Session) -> tuple[str, bool]:
    """Run a tool; returns (content, is_error). Errors are returned to the
    model as tool results, never raised into the run loop."""
    tool = _REGISTRY.get(name)
    if tool is None:
        return f"Error: unknown tool '{name}'", True
    try:
        # Savepoint per call: a DB error (oversize value, constraint) rolls
        # back this tool's writes only and comes back to the model as an
        # error result — without it the shared session stays failed and the
        # turn's commit kills the whole run (PendingRollbackError).
        with session.begin_nested() if session is not None else nullcontext():
            result = tool.handler(session, **tool_input)
        content = json.dumps(result, default=str, ensure_ascii=False)
        if len(content) > MAX_RESULT_CHARS:
            content = (
                content[:MAX_RESULT_CHARS]
                + f"\n… (result truncated at {MAX_RESULT_CHARS} chars — narrow the query for the rest)"
            )
        return content, False
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}", True


# ---------- core read tools ----------


def _idle_days(lead: Lead, today: date) -> int | None:
    return (today - lead.last_confirmed_action).days if lead.last_confirmed_action else None


def _list_open_leads(session: Session) -> list[dict]:
    today = date.today()
    leads = session.scalars(
        select(Lead)
        .where(Lead.stage.in_(OPEN_LEAD_STAGES), Lead.discarded_at.is_(None))
        .order_by(Lead.id)
    ).all()
    rows = [
        {
            "id": lead.id,
            "business_name": lead.business_name,
            "contact_name": lead.contact_name,
            "contact_email": lead.contact_email,
            "stage": lead.stage.value,
            "stage_since": lead.stage_since,
            "last_confirmed_action": lead.last_confirmed_action,
            "idle_days": _idle_days(lead, today),
            "pending_confirmation": lead.pending_confirmation,
        }
        for lead in leads
    ]
    rows.sort(key=lambda r: r["idle_days"] or -1, reverse=True)
    return rows


register(
    ToolDef(
        name="list_open_leads",
        description=(
            "List all open wholesale leads (stages new/contacted/sampled/negotiating) "
            "with stage, last confirmed action, idle days (sorted most-idle first), "
            "and any pending confirmation awaiting Arda."
        ),
        input_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        handler=_list_open_leads,
    )
)


def _list_tasks(session: Session, include_done: bool = False) -> list[dict]:
    stmt = select(Task).order_by(Task.due_date.is_(None), Task.due_date, Task.id)
    if not include_done:
        stmt = stmt.where(Task.status != TaskStatus.DONE)
    return [
        {
            "id": task.id,
            "title": task.title,
            "category": task.category.value,
            "status": task.status.value,
            "due_date": task.due_date,
            "priority": task.priority.value,
            "assignee": task.assignee,
            "waiting_on": task.waiting_on,
        }
        for task in session.scalars(stmt).all()
    ]


register(
    ToolDef(
        name="list_tasks",
        description=(
            "List board tasks across the five categories (wholesale_leads, consultation, "
            "pop_ups, invoice_tracking, governance). By default only incomplete tasks."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "include_done": {
                    "type": "boolean",
                    "description": "Also include completed tasks (pass false for only open ones).",
                }
            },
            "required": ["include_done"],
            "additionalProperties": False,
        },
        handler=_list_tasks,
    )
)
