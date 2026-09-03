from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.routines.tools  # noqa: F401
import app.tools.quickbooks as qbo
from app.engine.toolkit import clear_run_context, dispatch, set_run_context
from app.models import AppState, Base, Task, TaskStatus


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


# ---------- QuickBooks token rotation ----------


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


def test_refresh_rotates_and_persists_token_first(session_factory, monkeypatch):
    monkeypatch.setattr(qbo, "configured", lambda: True)
    monkeypatch.setenv("QBO_REALM_ID", "9999")
    # seed from env-style bootstrap
    with session_factory() as s:
        s.add(AppState(key=qbo.STATE_KEY, value={"refresh_token": "rt-old", "realm_id": "9999"}))

    calls = []

    def fake_post(url, headers=None, data=None, timeout=None):
        calls.append(data)
        return FakeResponse({"access_token": "at-1", "refresh_token": "rt-new"})

    monkeypatch.setattr(qbo.requests, "post", fake_post)
    access, realm = qbo.get_access_token(session_factory)

    assert access == "at-1" and realm == "9999"
    assert calls[0]["refresh_token"] == "rt-old"
    with session_factory() as s:
        stored = s.scalar(select(AppState).where(AppState.key == qbo.STATE_KEY))
        assert stored.value["refresh_token"] == "rt-new"  # rotated token persisted

    # next refresh uses the rotated token
    qbo.get_access_token(session_factory)
    assert calls[1]["refresh_token"] == "rt-new"


def test_not_connected_raises_cleanly(session_factory, monkeypatch):
    monkeypatch.setattr(qbo, "configured", lambda: True)
    monkeypatch.setattr(qbo.get_settings(), "qbo_refresh_token", None, raising=False)
    with pytest.raises(RuntimeError, match="not connected"):
        qbo.get_access_token(session_factory)


def test_parse_overdue_invoices():
    today = date(2026, 9, 3)
    payload = {
        "QueryResponse": {
            "Invoice": [
                {"Id": "1043", "DocNumber": "1043", "Balance": "420", "TotalAmt": "420",
                 "DueDate": "2026-08-20", "CustomerRef": {"name": "Halyard Café"}},
                {"Id": "1050", "DocNumber": "1050", "Balance": "0", "TotalAmt": "300",
                 "DueDate": "2026-08-01", "CustomerRef": {"name": "Paid Co"}},  # paid
                {"Id": "1051", "DocNumber": "1051", "Balance": "680", "TotalAmt": "680",
                 "DueDate": "2026-09-10", "CustomerRef": {"name": "Not Due"}},  # future
            ]
        }
    }
    rows = qbo.parse_overdue_invoices(payload, today)
    assert len(rows) == 1
    assert rows[0]["customer"] == "Halyard Café"
    assert rows[0]["days_overdue"] == 14


# ---------- update_task tool + board services ----------


def test_update_task_tool_moves_to_waiting(session_factory):
    import json

    set_run_context(run_id=None, routine_id=1, started_at=None)
    try:
        with session_factory() as s:
            task = Task(category="invoice_tracking", title="Chase #1043")
            s.add(task)
            s.flush()
            task_id = task.id
        with session_factory() as s:
            content, is_error = dispatch(
                "update_task",
                {"task_id": task_id, "status": "waiting", "waiting_on": "payment link — Third & Traction",
                 "due_date": None, "assignee": None},
                s,
            )
        assert not is_error and "waiting" in json.loads(content)["changes"][0]
        with session_factory() as s:
            task = s.get(Task, task_id)
            assert task.status == TaskStatus.WAITING
            assert "Third & Traction" in task.waiting_on
    finally:
        clear_run_context()


def test_board_services(session_factory, monkeypatch):
    import app.web.board_view as bv

    monkeypatch.setattr(bv, "db_session", session_factory)
    assert "Task added" in bv.create_task_manual("governance", "File quarterly sales tax", "2026-09-30", "Arda", "high")
    with session_factory() as s:
        task = s.query(Task).one()
        assert task.due_date == date(2026, 9, 30)
        task_id = task.id

    assert "→ done" in bv.set_task_status(task_id, "done")
    with session_factory() as s:
        assert s.get(Task, task_id).completed_at is not None

    boards = None  # load_boards uses global db_session; covered via services + template smoke


def test_qbo_status_not_configured(monkeypatch):
    monkeypatch.setattr(qbo.get_settings(), "qbo_client_id", None, raising=False)
    status = qbo.qbo_status()
    assert status["status"] == "not_configured"
