"""Nav badges, Won/Lost lanes, draft ordering, task↔lead linking, and
action-link extraction."""

from contextlib import contextmanager
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import (
    Base,
    DraftStatus,
    EmailDraft,
    Lead,
    LeadStage,
    Task,
    TaskCategory,
    TaskStatus,
)
from app.models.enums import FromMailbox
from app.web.action_links import extract_action_links

ADOBE = (
    "https://na3.documents.adobe.com/public/esign?tsid=CBFCIBAACBSCTBABDUAAABACAABAAKBXJY_"
    "UDn7ECe0NVqjJJsQqgG85ke9UfBcO5SjFlR6w-GXwDi9YSFVMMJGPdSnG3DF6vmQPAMJq1fPnOAvJnUZuID08&"
)


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


# ---------- action links ----------


def test_extracts_esign_link_with_label():
    messages = [{"body": f"Please sign here: {ADOBE} thanks!", "from": "Cindy Le", "date": "Thu"}]
    links = extract_action_links(messages)
    assert len(links) == 1
    assert links[0]["label"] == "Sign document · Adobe Sign"
    assert links[0]["url"].startswith("https://na3.documents.adobe.com/public/esign?tsid=")
    assert links[0]["from"] == "Cindy Le"


def test_dedupes_and_prefers_newest_mention():
    old = {"body": f"reminder {ADOBE}", "from": "old", "date": "Mon"}
    new = {"body": f"reminder again {ADOBE}", "from": "new", "date": "Fri"}
    links = extract_action_links([old, new])
    assert len(links) == 1
    assert links[0]["from"] == "new"  # newest message wins the card slot


def test_path_rules_and_ignores_ordinary_links():
    messages = [
        {
            "body": "pay https://www.paypal.com/invoice/p/abc "
            "profile https://www.paypal.com/company/about "
            "site https://boxxcoffee.com/menu",
            "from": "x",
            "date": "",
        }
    ]
    links = extract_action_links(messages)
    assert [l["label"] for l in links] == ["Pay invoice · PayPal"]


def test_strips_trailing_punctuation():
    messages = [{"body": "sign https://foo.docusign.net/signing/abc.", "from": "x", "date": ""}]
    (link,) = extract_action_links(messages)
    assert link["url"].endswith("/signing/abc")
    assert link["label"] == "Sign document · DocuSign"


# ---------- drafts: badge count + ordering ----------


def _draft(s, *, run_id=None, status=DraftStatus.COMPOSED, sent=False, subject="d"):
    d = EmailDraft(subject=subject, from_mailbox=FromMailbox.ARDA, status=status, run_id=run_id)
    if sent:
        d.sent_at = datetime.now(timezone.utc)
    s.add(d)
    s.flush()
    return d.id


def test_auto_ready_count_and_index_order(session_factory, monkeypatch):
    import app.web.drafts_view as dv

    monkeypatch.setattr(dv, "db_session", session_factory)
    with session_factory() as s:
        from app.models import Routine, Run
        from app.models.enums import RunStatus, RunTrigger

        routine = Routine(key="t", name="t", schedule_cron="0 8 * * *", system_prompt="p")
        s.add(routine)
        s.flush()
        run = Run(routine_id=routine.id, status=RunStatus.COMPLETED, trigger=RunTrigger.MANUAL)
        s.add(run)
        s.flush()
        sent_id = _draft(s, run_id=run.id, sent=True, subject="sent auto")
        manual_id = _draft(s, subject="manual")
        auto_id = _draft(s, run_id=run.id, subject="fresh auto")
        drafting_id = _draft(s, run_id=run.id, status=DraftStatus.DRAFTING, subject="in flight")

    # only composed, unsent, run-created drafts count for the badge
    assert dv.auto_ready_count() == 1

    index = [d["id"] for d in dv.load_drafts_index()]
    # unsent first (newest first within), the sent one last
    assert index == [drafting_id, auto_id, manual_id, sent_id]
    rows = {d["id"]: d for d in dv.load_drafts_index()}
    assert rows[auto_id]["auto"] is True
    assert rows[manual_id]["auto"] is False


# ---------- pipeline: won/lost lanes + overdue badge ----------


def test_board_gets_won_lost_lanes_and_overdue_count(session_factory, monkeypatch):
    import app.web.pipeline_view as pv

    monkeypatch.setattr(pv, "db_session", session_factory)
    with session_factory() as s:
        s.add(Lead(business_name="Open Overdue", stage=LeadStage.NEW, stage_since=date(2026, 8, 1)))
        s.add(
            Lead(
                business_name="Champion",
                stage=LeadStage.CLOSED_WON,
                stage_since=date(2026, 9, 1),
            )
        )
        s.add(
            Lead(
                business_name="Gone",
                stage=LeadStage.CLOSED_LOST,
                stage_since=date(2026, 8, 15),
                loss_reason="went with competitor",
            )
        )

    lanes, stats = pv.load_board()
    labels = [l["label"] for l in lanes]
    assert labels == ["New", "Contacted", "Sampled", "Negotiating", "Won", "Lost"]
    won = lanes[4]
    lost = lanes[5]
    assert won["closed"] and won["total"] == 1
    assert won["cards"][0]["name"] == "Champion"
    assert lost["cards"][0]["loss_reason"] == "went with competitor"
    assert stats["open"] == 1  # closed leads don't count as open

    assert pv.overdue_count() == 1  # the idle New lead


# ---------- board: link task to lead ----------


def test_todo_count_counts_all_categories(session_factory, monkeypatch):
    import app.web.board_view as bv

    monkeypatch.setattr(bv, "db_session", session_factory)
    with session_factory() as s:
        s.add(Task(category=TaskCategory.GOVERNANCE, title="a", status=TaskStatus.TODO))
        s.add(Task(category=TaskCategory.INVOICE_TRACKING, title="b", status=TaskStatus.TODO))
        s.add(Task(category=TaskCategory.WHOLESALE_LEADS, title="c", status=TaskStatus.DOING))
        s.add(Task(category=TaskCategory.POP_UPS, title="d", status=TaskStatus.DONE))
    assert bv.todo_count() == 2


# ---------- lead discard ----------


def test_discard_hides_lead_and_closes_its_tasks(session_factory, monkeypatch):
    import app.web.pipeline_view as pv

    monkeypatch.setattr(pv, "db_session", session_factory)
    with session_factory() as s:
        lead = Lead(business_name="Spam Cafe", stage=LeadStage.NEW, stage_since=date(2026, 8, 1))
        s.add(lead)
        s.flush()
        lead_id = lead.id
        s.add(
            Task(
                category=TaskCategory.WHOLESALE_LEADS,
                title="Qualify Spam Cafe",
                status=TaskStatus.TODO,
                source_ref={"lead_id": lead_id},
            )
        )

    assert pv.overdue_count() == 1
    msg = pv.discard_lead(lead_id)
    assert "Discarded Spam Cafe" in msg and "Qualify Spam Cafe" in msg

    lanes, stats = pv.load_board()
    assert all(not lane["cards"] for lane in lanes)  # gone from every lane
    assert stats["open"] == 0
    assert pv.overdue_count() == 0
    with session_factory() as s:
        assert s.query(Task).one().status == TaskStatus.DONE

    assert pv.discarded_leads()[0]["name"] == "Spam Cafe"
    assert "Restored" in pv.restore_lead(lead_id)
    assert pv.load_board()[1]["open"] == 1


def test_routines_cannot_resurrect_discarded_lead(session_factory):
    from datetime import datetime, timezone

    from app.routines.tools import _create_lead

    with session_factory() as s:
        s.add(
            Lead(
                business_name="Spam Cafe",
                contact_email="spam@x.com",
                stage=LeadStage.NEW,
                discarded_at=datetime.now(timezone.utc),
            )
        )
        s.commit()
        result = _create_lead(s, "Spam Cafe", "inbound_email", contact_email="other@x.com")
        assert result["outcome"] == "duplicate"
        assert "discarded" in result["note"]
        assert s.query(Lead).count() == 1


# ---------- automatic task -> lead linking ----------


def test_auto_link_by_confident_name_match(session_factory, monkeypatch):
    import app.web.board_view as bv
    from app.routines.task_sync import match_lead_for_task

    monkeypatch.setattr(bv, "db_session", session_factory)
    with session_factory() as s:
        s.add(Lead(business_name="LA Coffee Club", stage=LeadStage.CONTACTED))
        s.add(Lead(business_name="Golden Nook", stage=LeadStage.NEW))
        s.commit()

        matched = match_lead_for_task(s, "LA Coffee Club (Adam) — send order cutoff date")
        assert matched.business_name == "LA Coffee Club"
        assert match_lead_for_task(s, "File quarterly sales tax") is None
        # two unrelated names in one title -> ambiguous, no guess
        assert match_lead_for_task(s, "Intro LA Coffee Club to Golden Nook") is None

    msg = bv.create_task_manual(
        "wholesale_leads", "Send pricing to Golden Nook", "", "", "normal"
    )
    assert "Auto-linked to Golden Nook" in msg
    with session_factory() as s:
        task = s.query(Task).one()
        golden = s.query(Lead).filter(Lead.business_name == "Golden Nook").one()
        assert task.source_ref == {"lead_id": golden.id}


def test_auto_link_skips_discarded_and_short_names(session_factory):
    from datetime import datetime, timezone

    from app.routines.task_sync import match_lead_for_task

    with session_factory() as s:
        s.add(
            Lead(
                business_name="Umbral Collective",
                stage=LeadStage.NEW,
                discarded_at=datetime.now(timezone.utc),
            )
        )
        s.add(Lead(business_name="Ivy", stage=LeadStage.NEW))  # too short to trust
        s.commit()
        assert match_lead_for_task(s, "Call Umbral Collective about samples") is None
        assert match_lead_for_task(s, "Ivy budget review") is None


def test_startup_sweep_links_open_tasks(session_factory, monkeypatch):
    import app.db as app_db
    from app.routines.task_sync import auto_link_open_tasks

    monkeypatch.setattr(app_db, "db_session", session_factory)
    with session_factory() as s:
        s.add(Lead(business_name="RNT Coffee", stage=LeadStage.NEGOTIATING))
        s.add(Task(category=TaskCategory.WHOLESALE_LEADS, title="RNT Coffee — chase signed docs", status=TaskStatus.WAITING))
        s.add(Task(category=TaskCategory.GOVERNANCE, title="Renew insurance", status=TaskStatus.TODO))
        s.add(Task(category=TaskCategory.WHOLESALE_LEADS, title="RNT Coffee tasting", status=TaskStatus.DONE))

    assert auto_link_open_tasks() == 1  # waiting task linked; done + unmatched skipped
    with session_factory() as s:
        rnt = s.query(Lead).one()
        linked = [t for t in s.query(Task).all() if (t.source_ref or {}).get("lead_id") == rnt.id]
        assert len(linked) == 1 and linked[0].status == TaskStatus.WAITING


def test_create_task_tool_stores_contact_email(session_factory):
    from app.routines.tools import _create_task

    with session_factory() as s:
        result = _create_task(
            s, "governance", "TLR open items", "task:m-tlr",
            gmail_msg_id="m-tlr", contact_email="cindy@taxlawresearch.com",
        )
        s.commit()
        task = s.get(Task, result["task_id"])
        assert task.source_ref["contact_email"] == "cindy@taxlawresearch.com"
        assert task.source_ref["gmail_msg_id"] == "m-tlr"


def test_task_email_context_falls_back_to_contact_email(monkeypatch):
    import app.web.board_view as bv
    import app.web.pipeline_view as pv

    calls = []

    def fake_load_email_context(lead):
        calls.append(lead)
        return {"error": None, "messages": [{"from": "Cindy <cindy@tlr.com>", "date": "Fri"}],
                "label": "history", "thread_id": "t-9", "mailbox": "arda", "action_links": []}

    monkeypatch.setattr(pv, "load_email_context", fake_load_email_context)
    ctx = bv.load_task_email_context(
        {"lead_id": None, "gmail_msg_id": None, "contact_email": "cindy@tlr.com"}
    )
    assert calls == [{"contact_email": "cindy@tlr.com"}]
    assert ctx["reply_addr"] == "cindy@tlr.com"  # the To prefill for replies


def test_link_and_unlink_task_lead(session_factory, monkeypatch):
    import app.web.board_view as bv

    monkeypatch.setattr(bv, "db_session", session_factory)
    with session_factory() as s:
        lead = Lead(business_name="Golden Nook", stage=LeadStage.CONTACTED)
        task = Task(category=TaskCategory.GOVERNANCE, title="Sign agreement", status=TaskStatus.TODO)
        s.add_all([lead, task])
        s.flush()
        lead_id, task_id = lead.id, task.id

    msg = bv.link_task_to_lead(task_id, lead_id)
    assert "Golden Nook" in msg
    with session_factory() as s:
        assert s.get(Task, task_id).source_ref == {"lead_id": lead_id}

    assert "Already linked" in bv.link_task_to_lead(task_id, lead_id)

    assert "unlinked" in bv.unlink_task_lead(task_id)
    with session_factory() as s:
        assert (s.get(Task, task_id).source_ref or {}).get("lead_id") is None
