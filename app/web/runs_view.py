"""View-model builders for the Runs page (read-only transcript, spec §9)."""

import html
import json
from datetime import datetime

import markdown as _markdown
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.db import db_session
from app.models import MessageRole, Run, RunMessage
from app.settings import get_settings

TOOL_RESULT_PREVIEW_CHARS = 3000

# Mockup vernacular: R-041 for the tracker, A-118 for the agenda.
CODE_PREFIXES = {"lead_tracker": "R", "daily_agenda": "A"}


def render_markdown(text: str) -> str:
    """Assistant text -> safe HTML: escape first (no raw HTML passes through),
    then render markdown so the routines' bold/headers/tables display."""
    return _markdown.markdown(html.escape(text), extensions=["tables", "nl2br"])


def run_code(routine_key: str, run_id: int) -> str:
    prefix = CODE_PREFIXES.get(routine_key, (routine_key[:1] or "X").upper())
    return f"{prefix}-{run_id:03d}"


def _fmt_time(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    tz = ZoneInfo(get_settings().default_tz)
    if dt.tzinfo is None:
        from datetime import timezone

        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime("%d %b %H:%M").upper()


def build_transcript(messages) -> list[dict]:
    """Flatten run_messages content JSON into renderable blocks."""
    out = []
    for message in messages:
        role = message.role.value if isinstance(message.role, MessageRole) else str(message.role)
        entry = {"role": role, "time": _fmt_time(message.created_at), "blocks": []}
        content = message.content

        if role == "user":
            text = content.get("text", "") if isinstance(content, dict) else str(content)
            entry["blocks"].append({"kind": "text", "text": text})
        elif role == "assistant":
            for block in content if isinstance(content, list) else []:
                btype = block.get("type")
                if btype == "text" and block.get("text", "").strip():
                    entry["blocks"].append(
                        {"kind": "text", "text": block["text"], "html": render_markdown(block["text"])}
                    )
                elif btype == "tool_use":
                    args = json.dumps(block.get("input") or {}, ensure_ascii=False)
                    entry["blocks"].append(
                        {"kind": "tool_use", "name": block.get("name", "?"), "args": args}
                    )
                # thinking blocks arrive with empty text (display omitted) — skip
        else:  # tool results
            for result in content if isinstance(content, list) else []:
                text = str(result.get("content", ""))
                truncated = len(text) > TOOL_RESULT_PREVIEW_CHARS
                entry["blocks"].append(
                    {
                        "kind": "tool_result",
                        "content": text[:TOOL_RESULT_PREVIEW_CHARS] + ("\n… (truncated)" if truncated else ""),
                        "is_error": bool(result.get("is_error")),
                    }
                )

        if entry["blocks"]:
            out.append(entry)
    return out


def load_runs_index(limit: int = 40) -> list[dict]:
    try:
        with db_session() as s:
            runs = s.scalars(
                select(Run)
                .options(joinedload(Run.routine))
                .order_by(Run.started_at.desc(), Run.id.desc())
                .limit(limit)
            ).all()
            return [
                {
                    "id": run.id,
                    "code": run_code(run.routine.key, run.id),
                    "name": run.routine.name,
                    "time": _fmt_time(run.started_at),
                    "status": run.status.value,
                    "trigger": run.trigger.value,
                }
                for run in runs
            ]
    except Exception:
        return []


def load_transcript(run_id: int) -> tuple[dict | None, list[dict]]:
    try:
        with db_session() as s:
            run = s.get(Run, run_id, options=[joinedload(Run.routine)])
            if run is None:
                return None, []
            messages = s.scalars(
                select(RunMessage).where(RunMessage.run_id == run_id).order_by(RunMessage.id)
            ).all()
            selected = {
                "id": run.id,
                "code": run_code(run.routine.key, run.id),
                "name": run.routine.name,
                "status": run.status.value,
                "trigger": run.trigger.value,
                "started": _fmt_time(run.started_at),
                "completed": _fmt_time(run.completed_at),
                "error": run.error,
            }
            return selected, build_transcript(messages)
    except Exception:
        return None, []
