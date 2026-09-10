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


_CHECKLIST_LINE = re.compile(r"^\s*[-*]\s*\[( |x|X)\]\s*(.+)$")
_TASK_REF = re.compile(r"#(\d+)(?:\s*[–—-]\s*#?(\d+))?")
_MAX_RANGE = 30


def _ref_ids(text: str) -> list[int]:
    ids: list[int] = []
    for m in _TASK_REF.finditer(text):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        if start <= end and end - start < _MAX_RANGE:
            ids.extend(range(start, end + 1))
        else:
            ids.append(start)
    return list(dict.fromkeys(ids))


def extract_checklist(report: str) -> tuple[list[dict], str]:
    """Lift `- [ ] …` lines out of a briefing so they render as a live
    checklist backed by the board tasks they reference (#id / #a–#b ranges).
    Returns (items, report_without_those_lines). A header line directly above
    a run of checklist lines (e.g. "Act today") is lifted with them."""
    items: list[dict] = []
    kept: list[str] = []
    lines = report.splitlines()
    checklist_ix = {i for i, line in enumerate(lines) if _CHECKLIST_LINE.match(line)}

    def _next_nonblank(i: int) -> int:
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        return j

    for i, line in enumerate(lines):
        if i in checklist_ix:
            text = _CHECKLIST_LINE.match(line).group(2).strip()
            items.append({"text": text, "task_ids": _ref_ids(text)})
            continue
        stripped = line.strip()
        looks_like_header = stripped.startswith("#") or (
            stripped.startswith("**") and stripped.rstrip(":").endswith("**")
        ) or "act today" in stripped.lower()
        if _next_nonblank(i) in checklist_ix and (not stripped or looks_like_header):
            continue  # blank spacing or the checklist's own heading — lifted with it
        kept.append(line)
    return items, "\n".join(kept).strip()


def _with_task_state(session, items: list[dict]) -> list[dict]:
    """Attach live board state to checklist items: done when every referenced
    task is done; multi-task items carry per-task rows so they can expand and
    be cleared one by one. Items referencing no known task render inert."""
    from app.models import Task, TaskStatus

    all_ids = {tid for item in items for tid in item["task_ids"]}
    known: dict[int, tuple[str, bool]] = {}
    if all_ids:
        for task in session.scalars(select(Task).where(Task.id.in_(all_ids))):
            known[task.id] = (task.title, task.status == TaskStatus.DONE)
    out = []
    for item in items:
        tasks = [
            {"id": tid, "title": known[tid][0], "done": known[tid][1]}
            for tid in item["task_ids"]
            if tid in known
        ]
        out.append(
            {
                "text": item["text"],
                "html": render_markdown(item["text"]),
                "task_ids": [t["id"] for t in tasks],
                "tasks": tasks,
                "done_count": sum(1 for t in tasks if t["done"]),
                "done": bool(tasks) and all(t["done"] for t in tasks),
            }
        )
    return out


def load_todays_briefing(routine_key: str = "daily_agenda") -> dict | None:
    """Today's written agenda for the dashboard: the day's first completed
    agenda run (the morning briefing) with its "act today" checklist lifted
    out as live checkboxes backed by board tasks, plus the newest later run's
    report as a 'since then' update when there is one."""
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

            def shape(run, with_checklist=False):
                messages = s.scalars(
                    select(RunMessage).where(RunMessage.run_id == run.id).order_by(RunMessage.id)
                ).all()
                report = final_report(messages)
                if not report:
                    return None
                shaped = {
                    "run_id": run.id,
                    "code": run_code(routine_key, run.id),
                    "time": _fmt_time(run.started_at),
                }
                if with_checklist:
                    items, remaining = extract_checklist(report)
                    shaped["checklist"] = _with_task_state(s, items)
                    shaped["html"] = render_markdown(remaining) if remaining else ""
                else:
                    shaped["html"] = render_markdown(report)
                return shaped

            briefing = shape(todays[0], with_checklist=True)
            if briefing is None:
                return None
            latest = (
                shape(todays[-1], with_checklist=True)
                if todays[-1].id != todays[0].id
                else None
            )
            if latest:
                # Items a later run surfaced join the live checklist (deduped
                # by task id) instead of sitting as dead text in "since then".
                seen = {tid for item in briefing["checklist"] for tid in item["task_ids"]}
                for item in latest.pop("checklist", []):
                    ids = set(item["task_ids"])
                    if ids and ids <= seen:
                        continue
                    briefing["checklist"].append(item)
                    seen |= ids
            briefing["latest"] = latest
            return briefing
    except Exception:
        return None


def _fmt_cost(cost) -> str:
    return f"${float(cost):.2f}" if cost is not None else ""


def daily_costs(days: int = 14) -> dict:
    """Per-day run/cost log for the /costs page and its JSON twin: executed
    vs skipped runs, token totals (with the cached share), dollars, and a
    per-routine cost split over the window."""
    from datetime import timedelta, timezone as dt_timezone

    from app.models import RunStatus

    tz = ZoneInfo(get_settings().default_tz)
    today = datetime.now(tz).date()
    cutoff = today - timedelta(days=days - 1)

    def _aware(dt):
        return dt.replace(tzinfo=dt_timezone.utc) if dt is not None and dt.tzinfo is None else dt

    buckets: dict = {}
    routines: dict = {}
    try:
        with db_session() as s:
            runs = s.scalars(
                select(Run)
                .options(joinedload(Run.routine))
                .order_by(Run.started_at.desc(), Run.id.desc())
                .limit(1500)
            ).all()
            for run in runs:
                started = _aware(run.started_at)
                if started is None:
                    continue
                day = started.astimezone(tz).date()
                if day < cutoff or day > today:
                    continue
                b = buckets.setdefault(
                    day,
                    {"date": day, "runs": 0, "skipped": 0, "cost": 0.0,
                     "in_tokens": 0, "cached_tokens": 0, "out_tokens": 0},
                )
                if run.status == RunStatus.SKIPPED:
                    b["skipped"] += 1
                else:
                    b["runs"] += 1
                cost = float(run.cost_usd) if run.cost_usd is not None else 0.0
                b["cost"] += cost
                usage = run.usage or {}
                b["in_tokens"] += usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
                b["cached_tokens"] += usage.get("cache_read_input_tokens", 0)
                b["out_tokens"] += usage.get("output_tokens", 0)
                routines[run.routine.name] = routines.get(run.routine.name, 0.0) + cost
    except Exception:
        pass
    day_rows = sorted(buckets.values(), key=lambda b: b["date"], reverse=True)
    return {
        "days": day_rows,
        "routines": sorted(routines.items(), key=lambda kv: kv[1], reverse=True),
        "total": sum(b["cost"] for b in day_rows),
        "window_days": days,
    }


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
                    "cost": _fmt_cost(run.cost_usd),
                }
                for run in runs
            ]
    except Exception:
        return []


def spend_summary() -> dict:
    """API dollars spent today and over the trailing 7 days (LA), plus how
    many scheduled runs were skipped today by the quiet pre-flight."""
    tz = ZoneInfo(get_settings().default_tz)
    today = datetime.now(tz).date()
    out = {"today": 0.0, "week": 0.0, "skipped_today": 0}
    try:
        from datetime import timedelta, timezone as dt_timezone

        def _aware(dt):
            return dt.replace(tzinfo=dt_timezone.utc) if dt is not None and dt.tzinfo is None else dt

        with db_session() as s:
            # newest 600 comfortably covers 7 days of hourly runs
            runs = s.scalars(
                select(Run).order_by(Run.started_at.desc(), Run.id.desc()).limit(600)
            ).all()
            for run in runs:
                started = _aware(run.started_at)
                if started is None:
                    continue
                day = started.astimezone(tz).date()
                if day > today or day < today - timedelta(days=6):
                    continue
                cost = float(run.cost_usd) if run.cost_usd is not None else 0.0
                out["week"] += cost
                if day == today:
                    out["today"] += cost
                    from app.models import RunStatus

                    if run.status == RunStatus.SKIPPED:
                        out["skipped_today"] += 1
    except Exception:
        pass
    return out


def load_transcript(run_id: int) -> tuple[dict | None, list[dict]]:
    try:
        with db_session() as s:
            run = s.get(Run, run_id, options=[joinedload(Run.routine)])
            if run is None:
                return None, []
            messages = s.scalars(
                select(RunMessage).where(RunMessage.run_id == run_id).order_by(RunMessage.id)
            ).all()
            usage = run.usage or {}
            selected = {
                "id": run.id,
                "code": run_code(run.routine.key, run.id),
                "name": run.routine.name,
                "status": run.status.value,
                "trigger": run.trigger.value,
                "started": _fmt_time(run.started_at),
                "completed": _fmt_time(run.completed_at),
                "error": run.error,
                "cost": _fmt_cost(run.cost_usd),
                "usage_line": (
                    f"{usage.get('iterations', 0)} calls · "
                    f"{(usage.get('input_tokens', 0) + usage.get('cache_read_input_tokens', 0) + usage.get('cache_creation_input_tokens', 0)) / 1000:.0f}k in "
                    f"({usage.get('cache_read_input_tokens', 0) / 1000:.0f}k cached) · "
                    f"{usage.get('output_tokens', 0) / 1000:.1f}k out"
                )
                if usage
                else None,
            }
            return selected, build_transcript(messages)
    except Exception:
        return None, []
