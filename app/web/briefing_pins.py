"""Act-today pins: items Arda promoted from the agenda body into the briefing.

A pin is a task id stored in app_state. Pinned tasks appear in the Act today
checklist (tagged "pinned") until their task is done, and the runtime context
tells the model to keep carrying them — promotion is Arda's editorial call,
never demoted by a run.
"""

from sqlalchemy import select

from app.db import db_session

PINS_KEY = "briefing:pins"
MAX_PINS = 50


def pinned_ids(session) -> list[int]:
    from app.models import AppState

    row = session.get(AppState, PINS_KEY)
    ids = (row.value or {}).get("task_ids") if row else None
    return [int(i) for i in ids or [] if isinstance(i, (int, str)) and str(i).isdigit()]


def _store(session, ids: list[int]) -> None:
    from app.models import AppState

    row = session.get(AppState, PINS_KEY)
    value = {"task_ids": ids[-MAX_PINS:]}
    if row is None:
        session.add(AppState(key=PINS_KEY, value=value))
    else:
        row.value = value


def toggle_pin(task_id: int) -> dict:
    """Pin a task into Act today, or unpin if already pinned. Returns
    {ok, pinned, task_id, title, msg}."""
    from app.models import Task

    with db_session() as s:
        task = s.get(Task, task_id)
        if task is None:
            return {"ok": False, "pinned": False, "task_id": task_id, "title": "",
                    "msg": "Task not found."}
        ids = pinned_ids(s)
        if task_id in ids:
            ids = [i for i in ids if i != task_id]
            _store(s, ids)
            return {"ok": True, "pinned": False, "task_id": task_id, "title": task.title,
                    "msg": f"Unpinned from Act today: {task.title}."}
        ids.append(task_id)
        _store(s, ids)
        return {"ok": True, "pinned": True, "task_id": task_id, "title": task.title,
                "msg": f"Pinned to Act today: {task.title}."}


def open_pins(session) -> list:
    """Pinned tasks that are still open — and prune pins whose task is done
    or gone, so the list stays honest without a separate cleanup job."""
    from app.models import Task, TaskStatus

    ids = pinned_ids(session)
    if not ids:
        return []
    tasks = {
        t.id: t
        for t in session.scalars(select(Task).where(Task.id.in_(ids)))
    }
    kept, out = [], []
    for task_id in ids:
        task = tasks.get(task_id)
        if task is None or task.status == TaskStatus.DONE:
            continue  # pruned
        kept.append(task_id)
        out.append(task)
    if kept != ids:
        _store(session, kept)
    return out
