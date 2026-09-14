"""Cross-mailbox thread ids: Gmail ids are per-mailbox, so a thread id
captured in one mailbox must be remapped (or dropped) when drafting from the
other — never allowed to 404 the save/send."""

import pytest

import app.tools.gmail as gm
from app.models.enums import FromMailbox


class NotFound(Exception):
    def __init__(self):
        self.resp = type("R", (), {"status": 404})()


class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeSvc:
    """Just enough of the Gmail client: threads keyed by id, rfc822msgid search."""

    def __init__(self, threads=None, rfc_map=None):
        self.threads_by_id = threads or {}
        self.rfc_map = rfc_map or {}  # rfc822 id -> thread id

    def users(self):
        return self

    def threads(self):
        outer = self

        class T:
            def get(self, userId, id, **kw):
                def run():
                    if id not in outer.threads_by_id:
                        raise NotFound()
                    return outer.threads_by_id[id]

                return _Req(run)

        return T()

    def messages(self):
        outer = self

        class M:
            def list(self, userId, q, maxResults=1):
                def run():
                    rfc = q.split("rfc822msgid:", 1)[1]
                    tid = outer.rfc_map.get(rfc)
                    return {"messages": [{"id": "m", "threadId": tid}]} if tid else {}

                return _Req(run)

        return M()


def _wire(monkeypatch, hello: FakeSvc, arda: FakeSvc):
    monkeypatch.setattr(gm, "mailbox_address", lambda mb: f"{mb.value}@boxx.test")
    monkeypatch.setattr(
        gm, "gmail_service",
        lambda addr: hello if addr.startswith("hello") else arda,
    )


def _thread(rfc_id):
    return {"messages": [{"payload": {"headers": [{"name": "Message-ID", "value": rfc_id}]}}]}


def test_resolve_keeps_id_that_exists(monkeypatch):
    hello = FakeSvc(threads={"t-hello": {"messages": []}})
    _wire(monkeypatch, hello, FakeSvc())
    assert gm.resolve_thread_for_mailbox(FromMailbox.HELLO, "t-hello") == ("t-hello", None)


def test_resolve_remaps_other_mailboxes_id(monkeypatch):
    # id t-arda is arda@'s copy; hello@ holds the same conversation as t-hello
    hello = FakeSvc(rfc_map={"abc@mail": "t-hello"})
    arda = FakeSvc(threads={"t-arda": _thread("<abc@mail>")})
    _wire(monkeypatch, hello, arda)
    resolved, note = gm.resolve_thread_for_mailbox(FromMailbox.HELLO, "t-arda")
    assert resolved == "t-hello"
    assert "per-mailbox" in note


def test_resolve_clears_when_no_copy_exists(monkeypatch):
    hello = FakeSvc()  # no threads, rfc search finds nothing
    arda = FakeSvc(threads={"t-arda": _thread("<abc@mail>")})
    _wire(monkeypatch, hello, arda)
    resolved, note = gm.resolve_thread_for_mailbox(FromMailbox.HELLO, "t-arda")
    assert resolved is None and "exists only in arda@" in note

    _wire(monkeypatch, FakeSvc(), FakeSvc())  # nowhere at all
    resolved, note = gm.resolve_thread_for_mailbox(FromMailbox.HELLO, "ghost")
    assert resolved is None and "not found in either mailbox" in note


def test_reply_setup_degrades_missing_thread_to_new_email():
    svc = FakeSvc()  # any thread fetch 404s
    def raise_404(s, t):
        raise NotFound()
    orig = gm._thread_reply_headers
    gm._thread_reply_headers = lambda s, t: (_ for _ in ()).throw(NotFound())
    try:
        assert gm._reply_setup(svc, "ghost") == (None, None, None)
        assert gm._reply_setup(svc, None) == (None, None, None)
    finally:
        gm._thread_reply_headers = orig


def test_is_not_found_matches_only_404():
    assert gm.is_not_found(NotFound())
    assert not gm.is_not_found(Exception("boom"))
    e = Exception(); e.status_code = 404
    assert gm.is_not_found(e)
