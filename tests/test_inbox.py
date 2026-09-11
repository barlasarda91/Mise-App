from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.web.inbox_view as iv
from app.models import Base, MutedSender


@pytest.fixture
def session_factory(monkeypatch):
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

    monkeypatch.setattr(iv, "db_session", factory)
    return factory


@pytest.fixture(autouse=True)
def reset_cache():
    iv._count_cache.update(at=0.0, value=None)


def test_mute_unmute_roundtrip(session_factory):
    assert "Muted newsletter@spam.com" in iv.mute_sender(" Newsletter@Spam.com ")
    assert iv.muted_list() == ["newsletter@spam.com"]
    assert "already muted" in iv.mute_sender("newsletter@spam.com")
    assert "Not a valid address" in iv.mute_sender("junk")
    assert "Unmuted" in iv.unmute_sender("newsletter@spam.com")
    assert iv.muted_list() == []


def test_unread_query_negates_muted():
    q = iv.unread_query(["a@x.com", "b@y.com"])
    assert q.startswith("in:inbox is:unread")
    assert "-from:a@x.com" in q and "-from:b@y.com" in q
    assert iv.unread_query([]) == "in:inbox is:unread"


def test_load_inbox_merges_sorts_and_filters(session_factory, monkeypatch):
    import app.tools.gmail as gm

    iv.mute_sender("noise@spam.com")

    def fake_search(mailbox, query, after=None, max_results=25):
        assert "-from:noise@spam.com" in query
        if mailbox.value == "arda":
            return [
                {"id": "a1", "thread_id": "t1", "from": "Old <old@x.com>",
                 "date": "Mon, 01 Sep 2026 08:00:00 +0000", "subject": "old", "snippet": "s"},
            ]
        return [
            {"id": "h1", "thread_id": "t2", "from": "New <new@y.com>",
             "date": "Thu, 03 Sep 2026 08:00:00 +0000", "subject": "new", "snippet": "s"},
            {"id": "h2", "thread_id": "t3", "from": "Noise <noise@spam.com>",
             "date": "Thu, 03 Sep 2026 09:00:00 +0000", "subject": "spam", "snippet": "s"},
        ]

    monkeypatch.setattr(gm, "search_messages", fake_search)
    monkeypatch.setattr(iv, "sa_configured", lambda: True)
    data = iv.load_inbox()
    assert data["error"] is None
    assert [m["id"] for m in data["messages"]] == ["h1", "a1"]  # newest first, muted dropped
    assert data["messages"][0]["mailbox"] == "hello"
    assert data["messages"][0]["from_addr"] == "new@y.com"


def test_unread_count_caches(session_factory, monkeypatch):
    import app.tools.gmail as gm

    calls = []
    monkeypatch.setattr(gm, "count_messages", lambda mb, q: calls.append(mb.value) or 3)
    monkeypatch.setattr(iv, "sa_configured", lambda: True)

    assert iv.unread_count() == 6  # 3 + 3 across mailboxes
    assert iv.unread_count() == 6  # served from cache
    assert calls == ["arda", "hello"]  # only one round of API calls


def test_unread_count_none_when_unconfigured(monkeypatch):
    monkeypatch.setattr(iv, "sa_configured", lambda: False)
    assert iv.unread_count() is None


def test_make_task_from_thread_dedupes_and_links(session_factory, monkeypatch):
    import app.web.inbox_view as iv
    from app.models import Lead, LeadStage, Task

    monkeypatch.setattr(iv, "db_session", session_factory)
    with session_factory() as s:
        s.add(Lead(business_name="FE Design", stage=LeadStage.CONTACTED,
                   contact_email="eddie@fedesignandconsulting.com"))

    msg = iv.make_task_from_thread(
        "arda", "m-sow", "Eddie Navarrette", "eddie@fedesignandconsulting.com",
        "Boxx Coffee Roasters — SOW",
    )
    assert "Task created" in msg and "Auto-linked to FE Design" in msg

    again = iv.make_task_from_thread(
        "arda", "m-sow", "Eddie Navarrette", "eddie@fedesignandconsulting.com",
        "Boxx Coffee Roasters — SOW",
    )
    assert "Already on the board" in again

    with session_factory() as s:
        task = s.query(Task).one()
        assert task.source_ref["gmail_msg_id"] == "m-sow"
        assert task.source_ref["contact_email"] == "eddie@fedesignandconsulting.com"
        assert task.source_ref["lead_id"] is not None


def test_mark_awaiting_done_and_task_autodismiss(session_factory):
    from app.models import AwaitingDismissal

    assert "no thread id" in iv.mark_awaiting_done("arda", "")
    msg = iv.mark_awaiting_done("arda", "th-1", "Eddie")
    assert "Marked done — Eddie" in msg
    iv.mark_awaiting_done("arda", "th-1")  # idempotent upsert, no unique clash

    # → task also Done-marks its thread (including on the dedup path).
    iv.make_task_from_thread("arda", "m-x", "Steph", "steph@213filming.com",
                             "Filming", thread_id="th-film")
    iv.make_task_from_thread("arda", "m-x", "Steph", "steph@213filming.com",
                             "Filming", thread_id="th-film")
    with session_factory() as s:
        marks = {(d.mailbox, d.thread_id) for d in s.query(AwaitingDismissal)}
    assert marks == {("arda", "th-1"), ("arda", "th-film")}


def test_batch_awaiting_done_and_task(session_factory):
    from datetime import datetime, timezone

    from app.models import AwaitingDismissal, MailMessage, Task

    with session_factory() as s:
        s.add(MailMessage(mailbox="arda", gmail_msg_id="b1", thread_id="tb1",
                          from_addr="eddie@fed.com", from_name="Eddie", subject="SOW",
                          sent_at=datetime.now(timezone.utc), is_outbound=False))
        s.add(MailMessage(mailbox="hello", gmail_msg_id="b2", thread_id=None,
                          from_addr="steph@213filming.com", from_name="Steph", subject="Filming",
                          sent_at=datetime.now(timezone.utc), is_outbound=False))

    assert "tick some rows" in iv.batch_awaiting("done", [])
    assert "Unknown batch action" in iv.batch_awaiting("nuke", ["arda:b1"])

    msg = iv.batch_awaiting("done", ["arda:b1", "hello:b2", "arda:missing"])
    assert "Marked 2 done" in msg and "1 not found" in msg
    with session_factory() as s:
        marks = {(d.mailbox, d.thread_id) for d in s.query(AwaitingDismissal)}
    assert marks == {("arda", "tb1"), ("hello", "b2")}  # null thread falls back to msg id

    msg = iv.batch_awaiting("task", ["arda:b1", "hello:b2"])
    assert "2 task(s) created" in msg
    msg = iv.batch_awaiting("task", ["arda:b1"])
    assert "1 already on the board" in msg
    with session_factory() as s:
        titles = sorted(t.title for t in s.query(Task))
    assert titles == ["Reply to Eddie — SOW", "Reply to Steph — Filming"]
