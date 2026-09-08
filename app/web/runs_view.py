"""View-model builders for the Runs page (read-only transcript, spec §9)."""

import html
import json
import re
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


# Allow http(s)/mailto/single-slash-relative only; /(?!/) rejects
# protocol-relative //host links.
_UNSAFE_HREF = re.compile(r'href="(?!https?://|mailto:|/(?!/))[^"]*"', re.I)


def render_markdown(text: str) -> str:
    """Assistant text -> safe HTML: escape first (no raw HTML passes through),
    then render markdown so the routines' bold/headers/tables display. Link
    targets are restricted to http(s)/mailto/relative — markdown link syntax
    would otherwise let a javascript: href through the escaping."""
    rendered = _markdown.markdown(html.escape(text), extensions=["tables", "nl2br"])
    return _UNSAFE_HREF.sub('href="#"', rendered)


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


def final_report(messages) -> str:
    """The run's written report: text blocks of its last assistant message."""
    for message in reversed(list(messages)):
        role = message.role.value if isinstance(message.role, MessageRole) else str(message.role)
        if role != "assistant":
            continue
        content = message.content if isinstance(message.content, list) else []
        texts = [b.get("text", "") for b in content if b.get("type") == "text" and b.get("text", "").strip()]
        if texts:
            return "\n\n".join(texts)
    return ""


def load_todays_briefing(routine_key: str = "daily_agenda") -> dict | None:
    """Today's written agenda for the dashboard: the day's first completed
    agenda run (the morning briefing), plus the newest later run's report as
    a 'since then' update when there is one."""
    from datetime import time as dt_time, timezone as dt_timezone

    from app.models import Routine, RunStatus

    tz = ZoneInfo(get_settings().default_tz)
    day_start = datetime.combine(datetime.now(tz).date(), dt_time.min, tzinfo=tz)

    def _aware(dt):
        return dt.replace(tzinfo=dt_timezone.utc) if dt is not None and dt.tzinfo is None else dt

    try:
        with db_session() as s:
            runs = s.scalars(
                select(Run)
                .join(Routine, Run.routine_id == Routine.id)
                .where(Routine.key == routine_key, Run.status == RunStatus.COMPLETED)
                .order_by(Run.started_at, Run.id)
            ).all()
            todays = [r for r in runs if _aware(r.started_at) and _aware(r.started_at) >= day_start]
            if not todays:
                return None

            def shape(run):
                messages = s.scalars(
                    select(RunMessage).where(RunMessage.run_id == run.id).order_by(RunMessage.id)
                ).all()
                report = final_report(messages)
                if not report:
                    return None
                return {
                    "run_id": run.id,
                    "code": run_code(routine_key, run.id),
                    "time": _fmt_time(run.started_at),
                    "html": render_markdown(report),
                }

            briefing = shape(todays[0])
            if briefing is None:
                return None
            latest = shape(todays[-1]) if todays[-1].id != todays[0].id else None
            briefing["latest"] = latest
            return briefing
    except Exception:
        return None


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
