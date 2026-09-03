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
