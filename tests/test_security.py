"""Regression tests for the milestone-10 security review findings."""

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("APP_PASSWORD", "test-password")
os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ.setdefault("DEV_MODE", "1")

from app.main import app  # noqa: E402
from app.tools.gmail import build_mime, decode_mime  # noqa: E402
from app.web.runs_view import render_markdown  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app)


def test_security_headers_present(client):
    r = client.get("/login")
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"


def test_mime_headers_strip_crlf_injection():
    raw = build_mime(
        "hello@boxxcoffee.com",
        ["victim@x.com\r\nBcc: attacker@evil.com"],
        "Hi\r\nX-Injected: 1",
        "body",
        attachments=[{"filename": "../..\r\nname.pdf", "content_type": "application/pdf", "data": b"x"}],
    )
    msg = decode_mime(raw)
    assert msg["Bcc"] is None
    assert msg["X-Injected"] is None
    assert "victim@x.com" in msg["To"]
    assert "attacker" not in str(msg["Bcc"])  # injected recipient never became a header
    atts = list(msg.iter_attachments())
    assert "/" not in atts[0].get_filename() and "\n" not in atts[0].get_filename()


def test_markdown_blocks_javascript_hrefs():
    html_out = render_markdown("[click](javascript:alert(1)) and [ok](https://boxxcoffee.com)")
    assert "javascript:" not in html_out
    assert 'href="https://boxxcoffee.com"' in html_out
    assert "<script>" not in render_markdown("<script>alert(1)</script>")


def test_login_backoff_escalates(client, monkeypatch):
    import app.main as main

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    main._failed_logins["count"] = 0
    for _ in range(3):
        client.post("/login", data={"password": "wrong"})
    assert sleeps == [0.5, 1.0, 1.5]
    # success resets the counter
    client.post("/login", data={"password": "test-password"})
    assert main._failed_logins["count"] == 0


def test_login_refuses_default_secret_outside_dev(client, monkeypatch):
    from app.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "session_secret", "dev-secret-change-me", raising=False)
    monkeypatch.setattr(settings, "dev_mode", False, raising=False)
    r = client.post("/login", data={"password": "test-password"})
    assert r.status_code == 503
    assert "SESSION_SECRET" in r.text


def test_redirect_targets_validated(client):
    client.post("/login", data={"password": "test-password"})
    r = client.post(
        "/tasks/1/status",
        data={"status": "todo", "next": "https://evil.com"},
        follow_redirects=False,
    )
    assert r.headers["location"].startswith("/board")
    r = client.post(
        "/tasks/1/status",
        data={"status": "todo", "next": "//evil.com"},
        follow_redirects=False,
    )
    assert r.headers["location"].startswith("/board")
