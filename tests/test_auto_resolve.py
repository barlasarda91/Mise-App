"""Auto-resolve: email-derived tasks complete themselves once Boxx replied
on the source thread; money tasks only become verification candidates."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, MailMessage, Task, TaskActivity, TaskCategory, TaskStatus
from app.routines.task_sync import auto_resolve_replied_tasks, replied_money_tasks

NOW = datetime.now(timezone.utc)


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def factory():
        s = maker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    return factory


def _mail(s, msg_id, thread, hours_ago, outbound=False, mailbox="arda"):
    s.add(MailMessage(mailbox=mailbox, gmail_msg_id=msg_id, thread_id=thread,
                      from_addr="x@y.com", sent_at=NOW - timedelta(hours=hours_ago),
                      is_outbound=outbound))


def _task(s, title, msg_id, category=TaskCategory.GOVERNANCE, created_hours_ago=48):
    task = Task(category=category, title=title,
                source_ref={"gmail_msg_id": msg_id} if msg_id else None)
    s.add(task)
    s.flush()
    task.created_at = NOW - timedelta(hours=created_hours_ago)
    return task.id


def test_replied_task_auto_completes(session_factory):
    with session_factory() as s:
        _mail(s, "in1", "t1", hours_ago=50)                      # inquiry
        _mail(s, "out1", "t1", hours_ago=10, outbound=True)      # Arda replied
        done_id = _task(s, "Reply to filming inquiry", "in1")

        _mail(s, "in2", "t2", hours_ago=50)                      # never answered
        open_id = _task(s, "Reply to venue ask", "in2")

        # replied, but the counterparty wrote again afterwards -> stays open
        _mail(s, "in3", "t3", hours_ago=50)
        _mail(s, "out3", "t3", hours_ago=30, outbound=True)
        _mail(s, "in3b", "t3", hours_ago=5)
        reopened_id = _task(s, "Reply to Koki", "in3")

        # reply predates the task -> not evidence it was handled
        _mail(s, "in4", "t4", hours_ago=90)
        _mail(s, "out4", "t4", hours_ago=80, outbound=True)
        stale_id = _task(s, "Old thread task", "in4", created_hours_ago=48)

        resolved = auto_resolve_replied_tasks(s)
        s.commit()
        assert len(resolved) == 1 and f"[{done_id}]" in resolved[0]
        assert s.get(Task, done_id).status == TaskStatus.DONE
        for tid in (open_id, reopened_id, stale_id):
            assert s.get(Task, tid).status != TaskStatus.DONE
        act = s.query(TaskActivity).filter_by(task_id=done_id).one()
        assert act.type == "auto_resolved" and "auto-completed" in act.detail


def test_money_tasks_become_candidates_not_done(session_factory):
    with session_factory() as s:
        _mail(s, "b1", "tb", hours_ago=40)
        _mail(s, "b2", "tb", hours_ago=4, outbound=True)
        pay_id = _task(s, "Pay Aliso rent", "b1", category=TaskCategory.PAYMENTS)

        resolved = auto_resolve_replied_tasks(s)
        assert resolved == []
        candidates = replied_money_tasks(s)
        assert [t.id for t in candidates] == [pay_id]
        assert s.get(Task, pay_id).status != TaskStatus.DONE


def test_money_candidates_reach_agenda_context(session_factory):
    from app.engine.context import build_runtime_context
    from app.models import Routine

    with session_factory() as s:
        _mail(s, "b1", "tb", hours_ago=40)
        _mail(s, "b2", "tb", hours_ago=4, outbound=True)
        _task(s, "Pay Aliso rent", "b1", category=TaskCategory.PAYMENTS)
        agenda = Routine(key="daily_agenda", name="A", system_prompt="p")
        tracker = Routine(key="lead_tracker", name="T", system_prompt="p")
        s.add_all([agenda, tracker])
        s.flush()
        a_ctx = build_runtime_context(s, agenda)
        t_ctx = build_runtime_context(s, tracker)
    assert "NOT auto-completed" in a_ctx and "Pay Aliso rent" in a_ctx
    assert "NOT auto-completed" not in t_ctx
