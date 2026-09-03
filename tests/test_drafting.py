import json
from contextlib import contextmanager
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.routines.tools  # noqa: F401
from app.engine.drafter import build_system, generate_draft_job, lead_context
from app.engine.toolkit import clear_run_context, dispatch, set_run_context
from app.models import Base, DraftStatus, EmailDraft, ExternalMutation, Lead, LeadStage
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


class Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeClient:
    def __init__(self, payload=None, error=None):
        self.requests = []
        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.requests.append(kwargs)
                if error:
                    raise error
                return type("R", (), {"content": [Block(type="text", text=json.dumps(payload))]})()

        self.messages = _Messages()


def _seed_lead(session_factory):
    with session_factory() as s:
        lead = Lead(
            business_name="Ember Room",
            contact_name="Deniz Aksoy",
            contact_email="deniz@emberroom.la",
            stage=LeadStage.NEGOTIATING,
            last_confirmed_action=date(2026, 9, 1),
        )
        s.add(lead)
        s.flush()
        return lead.id


def test_voice_profiles_load_per_mailbox():
    arda = build_system(FromMailbox.ARDA)
    hello = build_system(FromMailbox.HELLO)
    assert "Best\nArda" in arda or "Best" in arda
    assert "never salesy" in arda.lower() or "sign-off" in arda.lower()
    assert "front-desk" in hello or "front desk" in hello
    assert "This is Arda from Boxx Coffee Roasters" in hello
    assert "Drafting rules" in arda and "Drafting rules" in hello


def test_generate_draft_job_fills_row(session_factory):
    lead_id = _seed_lead(session_factory)
    with session_factory() as s:
        draft = EmailDraft(
            subject="", from_mailbox=FromMailbox.ARDA,
            related_lead_id=lead_id, status=DraftStatus.DRAFTING,
            to_addrs=["deniz@emberroom.la"],
        )
        s.add(draft)
        s.flush()
        draft_id = draft.id

    client = FakeClient(
        payload={
            "to": ["deniz@emberroom.la"],
            "cc": [],
            "subject": "Boxx wholesale — Blend No:1 agreement",
            "body": "Hey Deniz,\n\nGreat speaking today.\n\nBest\nArda",
        }
    )
    generate_draft_job(draft_id, "send the agreement", client=client, session_factory=session_factory)

    with session_factory() as s:
        draft = s.get(EmailDraft, draft_id)
        assert draft.status == DraftStatus.COMPOSED
        assert draft.subject.startswith("Boxx wholesale")
        assert draft.body.endswith("Best\nArda")

    request = client.requests[0]
    assert "Ember Room" in request["messages"][0]["content"]  # lead context injected
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert "Voice A" in request["system"]


def test_generate_failure_is_visible_not_stuck(session_factory):
    with session_factory() as s:
        draft = EmailDraft(subject="", from_mailbox=FromMailbox.HELLO, status=DraftStatus.DRAFTING)
        s.add(draft)
        s.flush()
        draft_id = draft.id
    generate_draft_job(draft_id, "x", client=FakeClient(error=RuntimeError("api down")), session_factory=session_factory)
    with session_factory() as s:
        draft = s.get(EmailDraft, draft_id)
        assert draft.status == DraftStatus.COMPOSED  # editable, not stuck in drafting
        assert "generation failed" in draft.body


def test_save_to_gmail_and_update_flow(session_factory, monkeypatch):
    import app.web.drafts_view as dv

    monkeypatch.setattr(dv, "db_session", session_factory)
    calls = {}

    import app.tools.gmail as gm

    def fake_create(mailbox, to, subject, body, cc=None, thread_id=None, attachments=None):
        calls["create"] = dict(mailbox=mailbox, to=to, subject=subject, cc=cc, thread_id=thread_id)
        return {"draft_id": "gd-1", "message_id": "m-1", "thread_id": "t-9"}

    def fake_update(mailbox, draft_id, to, subject, body, cc=None, thread_id=None, attachments=None):
        calls["update"] = dict(draft_id=draft_id)
        return {"draft_id": "gd-1", "message_id": "m-2", "thread_id": "t-9"}

    monkeypatch.setattr(gm, "create_draft", fake_create)
    monkeypatch.setattr(gm, "update_draft", fake_update)

    with session_factory() as s:
        draft = EmailDraft(
            subject="Re: Wholesale", body="Hey,", from_mailbox=FromMailbox.HELLO,
            to_addrs=["lead@halcyon.la"], gmail_thread_id="t-9", status=DraftStatus.COMPOSED,
        )
        s.add(draft)
        s.flush()
        draft_id = draft.id

    msg = dv.save_to_gmail(draft_id)
    assert "hello@boxxcoffee.com" in msg
    assert calls["create"]["thread_id"] == "t-9"  # reply lands on-thread
    assert calls["create"]["mailbox"] == FromMailbox.HELLO
    with session_factory() as s:
        assert s.get(EmailDraft, draft_id).status == DraftStatus.SAVED_TO_GMAIL

    # editing after save drops back to composed; re-save updates the same Gmail draft
    dv.update_fields(draft_id, "hello", "lead@halcyon.la", "", "Re: Wholesale", "Hey — updated,")
    with session_factory() as s:
        assert s.get(EmailDraft, draft_id).status == DraftStatus.COMPOSED
    dv.save_to_gmail(draft_id)
    assert calls["update"]["draft_id"] == "gd-1"


def test_save_requires_to_and_subject(session_factory, monkeypatch):
    import app.web.drafts_view as dv

    monkeypatch.setattr(dv, "db_session", session_factory)
    with session_factory() as s:
        draft = EmailDraft(subject="", from_mailbox=FromMailbox.ARDA, status=DraftStatus.COMPOSED)
        s.add(draft)
        s.flush()
        draft_id = draft.id
    assert "To address" in dv.save_to_gmail(draft_id)


def test_load_thread_shapes_and_truncates(monkeypatch):
    import app.tools.gmail as gm
    from app.web.drafts_view import load_thread

    monkeypatch.setattr(
        gm, "get_thread_messages",
        lambda mailbox, thread_id, last_n=8: [
            {"from": "Tamara <t@cc.com>", "date": "Wed, 2 Sep", "subject": "Re: x", "body": "first"},
            {"from": "Arda", "date": "Thu, 3 Sep", "subject": "Re: x", "body": "y" * 10_000},
        ],
    )
    thread = load_thread({"thread_id": "t-1", "mailbox": "arda"})
    assert thread["error"] is None
    assert len(thread["messages"]) == 2
    assert thread["messages"][1]["body"].endswith("(truncated)")

    assert load_thread(None) is None
    assert thread["label"] == "thread"


def test_load_thread_falls_back_to_recent_history(monkeypatch):
    import app.tools.gmail as gm
    from app.web.drafts_view import load_thread

    monkeypatch.setattr(
        gm, "search_messages",
        lambda mailbox, query, after=None, max_results=25: [{"id": "m1", "thread_id": "t-hist"}]
        if query == "kati.isabel.m@gmail.com" else [],
    )
    monkeypatch.setattr(
        gm, "get_thread_messages",
        lambda mailbox, thread_id, last_n=8: [
            {"from": "Kathy", "date": "Mon", "subject": "Wholesale inquiry", "body": "Hi Boxx"}
        ] if thread_id == "t-hist" else [],
    )

    # no thread id, but history exists with the To address
    thread = load_thread({"thread_id": None, "mailbox": "hello", "to": "kati.isabel.m@gmail.com, other@x.com"})
    assert thread["label"] == "history"
    assert thread["messages"][0]["subject"] == "Wholesale inquiry"

    # no thread id and no history -> no panel
    assert load_thread({"thread_id": None, "mailbox": "hello", "to": "nobody@nowhere.com"}) is None
    # no addresses at all -> no panel
    assert load_thread({"thread_id": None, "mailbox": "arda", "to": ""}) is None


def test_load_thread_error_degrades(monkeypatch):
    import app.tools.gmail as gm
    from app.web.drafts_view import load_thread

    def boom(*a, **k):
        raise RuntimeError("no creds")

    monkeypatch.setattr(gm, "get_thread_messages", boom)
    thread = load_thread({"thread_id": "t-1", "mailbox": "hello"})
    assert "no creds" in thread["error"]
    assert thread["messages"] == []


def test_send_now_syncs_then_sends_and_locks(session_factory, monkeypatch):
    import app.web.drafts_view as dv

    monkeypatch.setattr(dv, "db_session", session_factory)
    calls = []

    import app.tools.gmail as gm

    monkeypatch.setattr(
        gm, "create_draft",
        lambda mailbox, to, subject, body, cc=None, thread_id=None, attachments=None: (
            calls.append("create") or {"draft_id": "gd-7", "message_id": "m", "thread_id": "t-1"}
        ),
    )
    monkeypatch.setattr(
        gm, "send_draft",
        lambda mailbox, draft_id: calls.append(("send", draft_id)) or {"message_id": "m2", "thread_id": "t-1"},
    )

    with session_factory() as s:
        draft = EmailDraft(
            subject="Boxx wholesale", body="Hey,\n\nBest\nArda", from_mailbox=FromMailbox.ARDA,
            to_addrs=["deniz@emberroom.la"], status=DraftStatus.COMPOSED,
        )
        s.add(draft)
        s.flush()
        draft_id = draft.id

    msg = dv.send_now(draft_id)
    assert msg == "Sent from ardabarlas@boxxcoffee.com."
    assert calls == ["create", ("send", "gd-7")]  # latest content synced, then sent
    with session_factory() as s:
        draft = s.get(EmailDraft, draft_id)
        assert draft.sent_at is not None
        assert draft.gmail_draft_id is None  # consumed by sending

    # sent drafts are locked
    assert "Already sent" in dv.send_now(draft_id)
    assert "Already sent" in dv.update_fields(draft_id, "arda", "x@y.com", "", "s", "b")


def test_send_advances_linked_lead(session_factory, monkeypatch):
    from datetime import date

    import app.tools.gmail as gm
    import app.web.drafts_view as dv
    from app.models import Lead, LeadActivity, LeadStage

    monkeypatch.setattr(dv, "db_session", session_factory)
    monkeypatch.setattr(
        gm, "create_draft",
        lambda mailbox, to, subject, body, cc=None, thread_id=None, attachments=None: {
            "draft_id": "gd-1", "message_id": "m-sent-1", "thread_id": "t-1"},
    )
    monkeypatch.setattr(
        gm, "send_draft", lambda mailbox, draft_id: {"message_id": "m-sent-1", "thread_id": "t-1"}
    )

    with session_factory() as s:
        lead = Lead(business_name="Buen Dia", stage=LeadStage.NEW, contact_email="j@buendia.com")
        s.add(lead)
        s.flush()
        draft = EmailDraft(
            subject="Boxx wholesale — samples", body="Hey Jonathan,", from_mailbox=FromMailbox.HELLO,
            to_addrs=["j@buendia.com"], related_lead_id=lead.id, status=DraftStatus.COMPOSED,
        )
        s.add(draft)
        s.flush()
        draft_id, lead_id = draft.id, lead.id

    msg = dv.send_now(draft_id)
    assert "Buen Dia → contacted" in msg
    with session_factory() as s:
        lead = s.get(Lead, lead_id)
        assert lead.stage == LeadStage.CONTACTED
        assert lead.last_confirmed_action == date.today()
        activities = s.query(LeadActivity).filter_by(lead_id=lead_id).all()
        types = sorted(a.type.value for a in activities)
        assert types == ["email_sent", "stage_change"]
        sent = next(a for a in activities if a.type.value == "email_sent")
        assert sent.gmail_msg_id == "m-sent-1"  # audit-idempotent


def test_send_resets_timer_without_stage_change_for_later_stages(session_factory, monkeypatch):
    from datetime import date

    import app.tools.gmail as gm
    import app.web.drafts_view as dv
    from app.models import Lead, LeadStage

    monkeypatch.setattr(dv, "db_session", session_factory)
    monkeypatch.setattr(
        gm, "create_draft",
        lambda mailbox, to, subject, body, cc=None, thread_id=None, attachments=None: {
            "draft_id": "gd-2", "message_id": "m-sent-2", "thread_id": None},
    )
    monkeypatch.setattr(
        gm, "send_draft", lambda mailbox, draft_id: {"message_id": "m-sent-2", "thread_id": None}
    )
    with session_factory() as s:
        lead = Lead(business_name="Ember Room", stage=LeadStage.NEGOTIATING,
                    last_confirmed_action=date(2026, 8, 20))
        s.add(lead)
        s.flush()
        draft = EmailDraft(subject="s", body="b", from_mailbox=FromMailbox.ARDA,
                           to_addrs=["d@e.la"], related_lead_id=lead.id, status=DraftStatus.COMPOSED)
        s.add(draft)
        s.flush()
        draft_id, lead_id = draft.id, lead.id

    msg = dv.send_now(draft_id)
    assert "timer reset" in msg
    with session_factory() as s:
        lead = s.get(Lead, lead_id)
        assert lead.stage == LeadStage.NEGOTIATING  # unchanged
        assert lead.last_confirmed_action == date.today()


def test_send_now_blocks_invalid_drafts(session_factory, monkeypatch):
    import app.web.drafts_view as dv

    monkeypatch.setattr(dv, "db_session", session_factory)
    with session_factory() as s:
        draft = EmailDraft(subject="", from_mailbox=FromMailbox.ARDA, status=DraftStatus.COMPOSED)
        s.add(draft)
        s.flush()
        draft_id = draft.id
    assert "To address" in dv.send_now(draft_id)


def test_attachments_flow_and_gmail_payload(session_factory, monkeypatch):
    import app.tools.gmail as gm
    import app.web.drafts_view as dv
    from app.models import DraftAttachment, StoredFile

    monkeypatch.setattr(dv, "db_session", session_factory)
    with session_factory() as s:
        draft = EmailDraft(
            subject="Boxx wholesale", body="Attached please see our pricelist,",
            from_mailbox=FromMailbox.HELLO, to_addrs=["kathy@umbral.com"],
            status=DraftStatus.COMPOSED,
        )
        s.add(draft)
        s.flush()
        draft_id = draft.id

    # upload + keep in library
    msg = dv.attach_upload(draft_id, "pricelist.pdf", b"%PDF-fake", "application/pdf", True, "wholesale pricelist")
    assert "Attached pricelist.pdf" in msg and "library" in msg
    assert dv.library_files()[0]["name"] == "wholesale pricelist"

    # ad-hoc upload, then remove -> orphan cleaned up
    dv.attach_upload(draft_id, "notes.txt", b"hi", "text/plain", False, "")
    detail = dv.load_draft(draft_id)
    assert [a["filename"] for a in detail["attachments"]] == ["pricelist.pdf", "notes.txt"]
    att_id = detail["attachments"][1]["id"]
    assert dv.remove_attachment(draft_id, att_id) == "Removed."
    with session_factory() as s:
        assert s.query(StoredFile).count() == 1  # library file kept, ad-hoc purged
        assert s.query(DraftAttachment).count() == 1

    # saving passes attachment payloads through to Gmail
    captured = {}

    def fake_create(mailbox, to, subject, body, cc=None, thread_id=None, attachments=None):
        captured["attachments"] = attachments
        return {"draft_id": "gd-1", "message_id": "m", "thread_id": None}

    monkeypatch.setattr(gm, "create_draft", fake_create)
    assert "Saved as a Gmail draft" in dv.save_to_gmail(draft_id)
    assert captured["attachments"][0]["filename"] == "pricelist.pdf"
    assert captured["attachments"][0]["data"] == b"%PDF-fake"


def test_attach_upload_size_limit(session_factory, monkeypatch):
    import app.web.drafts_view as dv

    monkeypatch.setattr(dv, "db_session", session_factory)
    with session_factory() as s:
        draft = EmailDraft(subject="x", from_mailbox=FromMailbox.ARDA, status=DraftStatus.COMPOSED)
        s.add(draft)
        s.flush()
        draft_id = draft.id
    msg = dv.attach_upload(draft_id, "big.pdf", b"x" * (11 * 1024 * 1024), "application/pdf", False, "")
    assert "too large" in msg


def test_build_mime_with_attachment_roundtrip():
    from app.tools.gmail import build_mime, decode_mime

    raw = build_mime(
        "hello@boxxcoffee.com", ["k@u.com"], "Pricelist", "body",
        attachments=[{"filename": "pricelist.pdf", "content_type": "application/pdf", "data": b"%PDF-fake"}],
    )
    msg = decode_mime(raw)
    atts = list(msg.iter_attachments())
    assert len(atts) == 1
    assert atts[0].get_filename() == "pricelist.pdf"
    assert atts[0].get_content() == b"%PDF-fake"


def test_extract_body_decodes_quoted_printable():
    import base64

    from app.tools.gmail import extract_body

    qp_text = "reach out direct=\nly if you have any questions. caf=C3=A9"
    payload = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Transfer-Encoding", "value": "quoted-printable"}],
        "body": {"data": base64.urlsafe_b64encode(qp_text.encode()).decode()},
    }
    text = extract_body(payload)
    assert "directly" in text
    assert "café" in text


def test_create_email_draft_tool_attaches_library_labels(session_factory):
    from app.models import DraftAttachment, StoredFile

    with session_factory() as s:
        s.add(StoredFile(filename="pl.pdf", content_type="application/pdf", size=9,
                         data=b"%PDF-fake", in_library=True, label="wholesale pricelist"))
    set_run_context(run_id=1, routine_id=1, started_at=datetime(2026, 9, 3, tzinfo=timezone.utc))
    try:
        with session_factory() as s:
            content, is_error = dispatch(
                "create_email_draft",
                {"mailbox": "hello", "to": ["k@u.com"], "subject": "s", "body": "b",
                 "purpose": "inquiry_reply", "attach_library_labels": ["wholesale pricelist", "nope"]},
                s,
            )
        result = json.loads(content)
        assert result["attached"] == ["wholesale pricelist"]
        assert result["missing_library_files"] == ["nope"]
        with session_factory() as s:
            assert s.query(DraftAttachment).count() == 1
    finally:
        clear_run_context()


def test_create_email_draft_tool_dedups_per_run(session_factory):
    lead_id = _seed_lead(session_factory)
    set_run_context(run_id=42, routine_id=1, started_at=datetime(2026, 9, 3, tzinfo=timezone.utc))
    try:
        args = dict(
            mailbox="arda", to=["deniz@emberroom.la"], cc=None,
            subject="Boxx wholesale — agreement", body="Hey Deniz,\n\nBest\nArda",
            purpose="followup", lead_id=lead_id, task_id=None, gmail_thread_id=None,
        )
        with session_factory() as s:
            first = json.loads(dispatch("create_email_draft", dict(args), s)[0])
        with session_factory() as s:
            second = json.loads(dispatch("create_email_draft", dict(args), s)[0])
        assert first["outcome"] == "created"
        assert second["outcome"] == "already_exists"
        with session_factory() as s:
            assert s.query(EmailDraft).count() == 1
            assert s.query(ExternalMutation).count() == 1
            draft = s.query(EmailDraft).one()
            assert draft.status == DraftStatus.COMPOSED
            assert draft.run_id == 42
    finally:
        clear_run_context()
