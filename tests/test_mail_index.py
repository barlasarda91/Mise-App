"""Mail index: backfill/upkeep upserts and the awaiting-reply memory."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.tools.mail_index as mi
from app.models import Base, DisregardRule, MailMessage, MutedSender, Routine
from app.models.enums import FromMailbox


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


def _summary(msg_id, frm, date, thread="t1", to="ardabarlas@boxxcoffee.com", subject="Filming at Boxx"):
    return {"id": msg_id, "thread_id": thread, "from": frm, "to": to,
            "subject": subject, "snippet": "snippet…", "date": date}


def test_upsert_dedupes_and_flags_outbound(session_factory):
    with session_factory() as s:
        assert mi.upsert_summary(s, "arda", _summary("m1", "Steph <steph@213filming.com>", "Mon, 07 Sep 2026 10:00:00 -0700"))
        assert not mi.upsert_summary(s, "arda", _summary("m1", "Steph <steph@213filming.com>", "Mon, 07 Sep 2026 10:00:00 -0700"))
        assert mi.upsert_summary(s, "arda", _summary("m2", "Arda <ardabarlas@boxxcoffee.com>", "Mon, 07 Sep 2026 11:00:00 -0700"))
        s.commit()
        rows = {r.gmail_msg_id: r for r in s.query(MailMessage)}
        assert rows["m1"].is_outbound is False and rows["m1"].from_addr == "steph@213filming.com"
        assert rows["m2"].is_outbound is True
        assert rows["m1"].sent_at is not None


def _days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%a, %d %b %Y %H:%M:%S +0000")


def test_awaiting_reply_finds_hanging_threads(session_factory):
    with session_factory() as s:
        # filming thread: Steph spoke last -> awaiting
        mi.upsert_summary(s, "arda", _summary("f1", "Steph <steph@213filming.com>", _days_ago(20), thread="film"))
        mi.upsert_summary(s, "arda", _summary("f2", "Arda <ardabarlas@boxxcoffee.com>", _days_ago(18), thread="film"))
        mi.upsert_summary(s, "arda", _summary("f3", "Steph <steph@213filming.com>", _days_ago(5), thread="film"))
        # answered thread: Boxx replied last -> not awaiting
        mi.upsert_summary(s, "hello", _summary("a1", "Ben <ben@sidestream.ai>", _days_ago(4), thread="side"))
        mi.upsert_summary(s, "hello", _summary("a2", "Boxx <hello@boxxcoffee.com>", _days_ago(3), thread="side"))
        # muted + noreply + disregarded senders excluded
        mi.upsert_summary(s, "arda", _summary("n1", "Robot <noreply@spamco.com>", _days_ago(9), thread="spam"))
        mi.upsert_summary(s, "arda", _summary("mu1", "Newsletterer <news@letter.com>", _days_ago(9), thread="news"))
        mi.upsert_summary(s, "arda", _summary("d1", "Vetoed <vetoed@x.com>", _days_ago(9), thread="veto"))
        s.add(MutedSender(email="news@letter.com"))
        s.add(DisregardRule(contact_email="vetoed@x.com", title="vetoed"))
        s.commit()

        waiting = mi.awaiting_reply(s)
    assert [t["thread_id"] for t in waiting] == ["film"]
    assert waiting[0]["from_addr"] == "steph@213filming.com"
    assert waiting[0]["age_days"] >= 4
    assert waiting[0]["gmail_msg_id"] == "f3"


def test_index_recent_needs_existing_index(session_factory, monkeypatch):
    monkeypatch.setattr(mi, "db_session", session_factory)
    calls = []
    monkeypatch.setattr("app.tools.gmail.search_page",
                        lambda mb, q, page_token=None, page_size=100: (calls.append(q) or ([], None)))
    assert mi.index_recent(FromMailbox.ARDA) == 0
    assert calls == []  # empty index: upkeep leaves the build to the backfill

    with session_factory() as s:
        mi.upsert_summary(s, "arda", _summary("m1", "x <x@y.com>", _days_ago(2)))
        s.commit()
    monkeypatch.setattr(
        "app.tools.gmail.search_page",
        lambda mb, q, page_token=None, page_size=100: ([
            _summary("m1", "x <x@y.com>", _days_ago(2)),   # already indexed
            _summary("m9", "z <z@y.com>", _days_ago(1)),   # new
        ], None),
    )
    assert mi.index_recent(FromMailbox.ARDA) == 1


def test_backfill_pages_and_records_state(session_factory, monkeypatch):
    monkeypatch.setattr(mi, "db_session", session_factory)
    pages = {
        None: ([_summary("p1", "a <a@b.c>", _days_ago(3))], "tok2"),
        "tok2": ([_summary("p2", "b <b@b.c>", _days_ago(2))], None),
    }
    monkeypatch.setattr(
        "app.tools.gmail.search_page",
        lambda mb, q, page_token=None, page_size=100: (
            pages.get(page_token, ([], None)) if mb == FromMailbox.ARDA else ([], None)
        ),
    )
    totals = mi.backfill()
    assert totals["indexed"] == 2  # both pages of arda's mailbox; hello empty
    status = mi.index_status()
    assert status["backfill"]["state"] == "done"
    assert status["rows"] >= 2


def test_context_carries_awaiting_reply(session_factory):
    from app.engine.context import build_runtime_context

    with session_factory() as s:
        mi.upsert_summary(s, "arda", _summary("f0", "Arda <ardabarlas@boxxcoffee.com>", _days_ago(8), thread="film", to="steph@213filming.com"))
        mi.upsert_summary(s, "arda", _summary("f1", "Steph <steph@213filming.com>", _days_ago(6), thread="film"))
        routine = Routine(key="daily_agenda", name="Daily Agenda", system_prompt="p",
                          connectors=["gmail_arda", "calendar"])
        s.add(routine)
        s.flush()
        context = build_runtime_context(s, routine)
    assert "Threads awaiting a Boxx reply" in context
    assert "Filming at Boxx" in context and "steph" in context.lower() or "Steph" in context


def test_awaiting_reply_filters_bulk_and_strangers(session_factory):
    with session_factory() as s:
        # newsletter with bulk header -> excluded
        mi.upsert_summary(s, "arda", {**_summary("b1", "Scott Rao <newsletter-random@srao.com>", _days_ago(10), thread="rao"), "bulk": True})
        # notification-shaped address -> excluded even without bulk header
        mi.upsert_summary(s, "arda", _summary("b2", "Chase <alerts@chase.com>", _days_ago(10), thread="chase"))
        # stranger we've never written to (cold marketing from a personal-looking addr) -> excluded
        mi.upsert_summary(s, "arda", _summary("b3", "Randy Sales <randy@coldpitch.io>", _days_ago(10), thread="cold"))
        # real correspondent: Boxx wrote to Steph in another thread earlier
        mi.upsert_summary(s, "arda", _summary("o1", "Arda <ardabarlas@boxxcoffee.com>", _days_ago(30), thread="old", to="steph@213filming.com"))
        mi.upsert_summary(s, "arda", _summary("r1", "Steph <steph@213filming.com>", _days_ago(8), thread="film"))
        # real thread where we replied earlier IN-thread, counterparty spoke last
        mi.upsert_summary(s, "hello", _summary("e1", "Boxx <hello@boxxcoffee.com>", _days_ago(12), thread="inv", to="andres@shippingco.com"))
        mi.upsert_summary(s, "hello", _summary("e2", "Andres Eger <andres@shippingco.com>", _days_ago(9), thread="inv"))
        s.commit()

        waiting = mi.awaiting_reply(s)
    got = {t["from_addr"] for t in waiting}
    assert got == {"steph@213filming.com", "andres@shippingco.com"}


def test_resweep_backfills_bulk_flag(session_factory):
    with session_factory() as s:
        # first sweep predates the flag (no "bulk" key)
        mi.upsert_summary(s, "arda", _summary("m1", "News <hi@list.example.com>", _days_ago(5)))
        s.commit()
        assert s.query(MailMessage).one().is_bulk is False
        # re-sweep sees the same message, now with header data
        assert not mi.upsert_summary(s, "arda", {**_summary("m1", "News <hi@list.example.com>", _days_ago(5)), "bulk": True})
        s.commit()
        assert s.query(MailMessage).one().is_bulk is True


def test_awaiting_reply_respects_done_marks(session_factory):
    from datetime import datetime, timedelta, timezone

    from app.models import AwaitingDismissal

    with session_factory() as s:
        mi.upsert_summary(s, "arda", _summary("g1", "Arda <ardabarlas@boxxcoffee.com>", _days_ago(31), thread="sow", to="eddie@fed.com"))
        mi.upsert_summary(s, "arda", _summary("g2", "Eddie <eddie@fed.com>", _days_ago(30), thread="sow"))
        s.commit()
        assert [t["thread_id"] for t in mi.awaiting_reply(s)] == ["sow"]

        # Done-mark (a little in the past, so the resurfacing message below is
        # unambiguously newer despite second-granularity date headers).
        s.add(AwaitingDismissal(mailbox="arda", thread_id="sow",
                                dismissed_at=datetime.now(timezone.utc) - timedelta(minutes=5)))
        s.commit()
        assert mi.awaiting_reply(s) == []

        # Eddie writes again after the dismissal: thread resurfaces.
        mi.upsert_summary(s, "arda", _summary("g3", "Eddie <eddie@fed.com>", _days_ago(0), thread="sow"))
        s.commit()
        waiting = mi.awaiting_reply(s)
    assert [t["gmail_msg_id"] for t in waiting] == ["g3"]


def test_colleague_mail_is_inbound_not_outbound(session_factory):
    """Regression: Emre's to-do email to Arda was classified as Boxx's own
    sent mail (any @boxxcoffee.com sender counted as outbound), so it never
    reached the awaiting panel. Only the two connected mailboxes count."""
    with session_factory() as s:
        mi.upsert_summary(s, "arda", _summary(
            "e1", "Emre Barlas <emrebarlas@boxxcoffee.com>", _days_ago(2),
            thread="t-emre", subject="Weekly to do list"))
        mi.upsert_summary(s, "arda", _summary(
            "o1", "Arda <ardabarlas@boxxcoffee.com>", _days_ago(30),
            thread="t-old", to="emrebarlas@boxxcoffee.com", subject="re: roast plan"))
        s.commit()
        rows = {r.gmail_msg_id: r for r in s.query(MailMessage)}
        assert rows["e1"].is_outbound is False  # colleague = correspondent
        assert rows["o1"].is_outbound is True   # our own mailbox

        # and his hanging thread now shows in the awaiting panel (Boxx has
        # written to him before, so the correspondent test passes)
        waiting = mi.awaiting_reply(s)
        assert any(t["from_addr"] == "emrebarlas@boxxcoffee.com" for t in waiting)


def test_reclassify_outbound_repairs_old_rows(session_factory, monkeypatch):
    monkeypatch.setattr(mi, "db_session", session_factory)
    with session_factory() as s:
        # rows written under the old domain rule
        s.add(MailMessage(mailbox="arda", gmail_msg_id="old1", thread_id="t1",
                          from_addr="emrebarlas@boxxcoffee.com", is_outbound=True))
        s.add(MailMessage(mailbox="arda", gmail_msg_id="old2", thread_id="t2",
                          from_addr="travishe@boxxcoffee.com", is_outbound=True))
        s.add(MailMessage(mailbox="arda", gmail_msg_id="old3", thread_id="t3",
                          from_addr="ardabarlas@boxxcoffee.com", is_outbound=True))
        s.add(MailMessage(mailbox="hello", gmail_msg_id="old4", thread_id="t4",
                          from_addr="customer@gmail.com", is_outbound=False))
    assert mi.reclassify_outbound() == 2  # the two colleagues flip to inbound
    assert mi.reclassify_outbound() == 0  # one-time: flagged in app_state
    with session_factory() as s:
        rows = {r.gmail_msg_id: r.is_outbound for r in s.query(MailMessage)}
        assert rows == {"old1": False, "old2": False, "old3": True, "old4": False}


def test_colleague_reply_no_longer_auto_resolves_tasks(session_factory):
    """A colleague sending mail on a tracked thread must not count as 'Boxx
    already replied' — before the fix it silently completed open tasks."""
    from app.models import Task
    from app.models.enums import TaskCategory, TaskStatus
    from app.routines.task_sync import auto_resolve_replied_tasks

    with session_factory() as s:
        task = Task(title="Review weekly plan", category=TaskCategory.GOVERNANCE,
                    source_ref={"gmail_msg_id": "e1"})
        s.add(task)
        mi.upsert_summary(s, "arda", _summary(
            "e1", "Emre Barlas <emrebarlas@boxxcoffee.com>", _days_ago(3), thread="t-emre"))
        mi.upsert_summary(s, "arda", _summary(
            "e2", "Emre Barlas <emrebarlas@boxxcoffee.com>", _days_ago(1), thread="t-emre"))
        s.commit()
        resolved = auto_resolve_replied_tasks(s)
        s.commit()
        assert resolved == []
        assert s.get(Task, task.id).status != TaskStatus.DONE


def test_new_messages_since_surfaces_strangers(session_factory):
    """Regression for slow wholesale pickup: the awaiting panel's
    correspondent test hides first-time senders, so runs get an unfiltered
    NEW-mail queue instead. Strangers are the point."""
    with session_factory() as s:
        mi.upsert_summary(s, "hello", _summary(
            "w1", "Nina <nina@kcmconnect.co>", _days_ago(0),
            thread="t-w1", to="hello@boxxcoffee.com",
            subject="Wholesale Opportunity - Dolce by Tina"))
        mi.upsert_summary(s, "hello", dict(_summary(
            "n1", "Promo <blast@news.example>", _days_ago(0), thread="t-n1"),
            bulk=True))
        mi.upsert_summary(s, "hello", _summary(
            "nr1", "Robot <noreply@something.example>", _days_ago(0), thread="t-nr"))
        mi.upsert_summary(s, "hello", _summary(
            "old1", "Earlier <earlier@x.example>", _days_ago(3), thread="t-old"))
        mi.upsert_summary(s, "hello", _summary(
            "out1", "Us <hello@boxxcoffee.com>", _days_ago(0), thread="t-out"))
        s.commit()
        since = datetime.now(timezone.utc) - timedelta(days=1)
        fresh = mi.new_messages_since(s, "hello", since)
    ids = [m["gmail_msg_id"] for m in fresh]
    assert ids == ["w1"]  # stranger kept; bulk, noreply-shaped, old, own mail out
    assert fresh[0]["from_addr"] == "nina@kcmconnect.co"


def test_new_mail_queue_reaches_both_routines_context(session_factory):
    from app.engine.context import build_runtime_context
    from app.models import Routine, SyncState
    from app.models.enums import SyncSource

    with session_factory() as s:
        agenda = Routine(key="daily_agenda", name="A", system_prompt="p",
                         connectors=["gmail_arda", "gmail_hello", "calendar"])
        tracker = Routine(key="lead_tracker", name="T", system_prompt="p",
                          connectors=["gmail_arda", "gmail_hello", "calendar"])
        s.add_all([agenda, tracker])
        s.flush()
        for routine in (agenda, tracker):
            s.add(SyncState(routine_id=routine.id, source=SyncSource.GMAIL_HELLO,
                            last_run_at=datetime.now(timezone.utc) - timedelta(hours=2)))
        mi.upsert_summary(s, "hello", _summary(
            "w2", "Nina <nina@kcmconnect.co>", _days_ago(0), thread="t-w2",
            to="hello@boxxcoffee.com", subject="Wholesale Opportunity"))
        s.commit()
        a_ctx = build_runtime_context(s, agenda)
        t_ctx = build_runtime_context(s, tracker)

    for ctx in (a_ctx, t_ctx):
        assert "NEW mail since your last gather (1)" in ctx
        assert "nina@kcmconnect.co" in ctx and "Wholesale Opportunity" in ctx
        assert "OUTRANK everything else this run" in ctx
        assert "never skip one silently" in ctx


def test_new_mail_queue_absent_on_cold_start(session_factory):
    from app.engine.context import build_runtime_context
    from app.models import Routine

    with session_factory() as s:
        agenda = Routine(key="daily_agenda", name="A", system_prompt="p",
                         connectors=["gmail_arda", "gmail_hello"])
        s.add(agenda)
        s.flush()
        mi.upsert_summary(s, "hello", _summary(
            "w3", "Nina <nina@kcmconnect.co>", _days_ago(0), thread="t-w3"))
        s.commit()
        assert "NEW mail since your last gather" not in build_runtime_context(s, agenda)


def test_form_notification_reattributed_to_customer(session_factory):
    """Website wholesale-form notifications are sent FROM hello@ back into
    the inbox with the customer in To/Reply-To. They must index as INBOUND
    mail from the customer — while Boxx's actual replies (same From, same
    subject, but no INBOX label) stay outbound."""
    with session_factory() as s:
        # the notification: from us, INBOX label, customer in To
        assert mi.upsert_summary(s, "hello", dict(_summary(
            "f1", "Boxx Coffee Roasters <hello@boxxcoffee.com>", _days_ago(0),
            thread="t-form", to="Matthew Walker <matt@cobis.la>",
            subject="New Wholesale Inquiry — Matthew Walker"),
            label_ids=["IMPORTANT", "CATEGORY_UPDATES", "INBOX"]))
        # our actual reply on the same thread: SENT only
        assert mi.upsert_summary(s, "hello", dict(_summary(
            "f2", "Boxx Coffee Roasters <hello@boxxcoffee.com>", _days_ago(0),
            thread="t-form", to="matt@cobis.la",
            subject="New Wholesale Inquiry — Matthew Walker"),
            label_ids=["SENT"]))
        # a self-addressed note stays outbound (counterparty is ourselves)
        assert mi.upsert_summary(s, "hello", dict(_summary(
            "f3", "hello@boxxcoffee.com", _days_ago(0),
            thread="t-self", to="hello@boxxcoffee.com", subject="Order record"),
            label_ids=["SENT", "INBOX"]))
        s.commit()
        rows = {r.gmail_msg_id: r for r in s.query(MailMessage)}
        assert rows["f1"].is_outbound is False
        assert rows["f1"].from_addr == "matt@cobis.la"
        assert rows["f1"].from_name == "Matthew Walker"
        assert rows["f2"].is_outbound is True
        assert rows["f3"].is_outbound is True

        # and the notification reaches the NEW-mail triage queue
        since = datetime.now(timezone.utc) - timedelta(days=1)
        fresh = mi.new_messages_since(s, "hello", since)
        assert [m["gmail_msg_id"] for m in fresh] == ["f1"]
        assert fresh[0]["from_addr"] == "matt@cobis.la"


def test_form_notification_prefers_reply_to(session_factory):
    with session_factory() as s:
        mi.upsert_summary(s, "hello", dict(_summary(
            "f4", "hello@boxxcoffee.com", _days_ago(0), thread="t-f4",
            to="hello@boxxcoffee.com",
            subject="New Wholesale Inquiry — Sarah Abdow"),
            reply_to="Sarah Abdow <info@everydayeats.co>",
            label_ids=["INBOX"]))
        s.commit()
        row = s.query(MailMessage).one()
        assert row.is_outbound is False
        assert row.from_addr == "info@everydayeats.co"


def test_reseeing_a_row_repairs_old_attribution(session_factory):
    """repair_form_inquiries re-sweeps own-sender inbox mail; upsert must
    correct rows indexed under the old from-address rule."""
    with session_factory() as s:
        # old row: indexed as outbound from hello@
        s.add(MailMessage(mailbox="hello", gmail_msg_id="f5", thread_id="t-f5",
                          from_addr="hello@boxxcoffee.com", is_outbound=True,
                          subject="New Wholesale Inquiry — Vanessa Morales"))
        s.commit()
        mi.upsert_summary(s, "hello", dict(_summary(
            "f5", "hello@boxxcoffee.com", _days_ago(10), thread="t-f5",
            to="vanessa@casasirias.com",
            subject="New Wholesale Inquiry — Vanessa Morales"),
            label_ids=["INBOX", "CATEGORY_PERSONAL"]))
        s.commit()
        row = s.query(MailMessage).one()
        assert row.is_outbound is False and row.from_addr == "vanessa@casasirias.com"
