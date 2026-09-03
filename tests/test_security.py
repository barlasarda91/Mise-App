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


def test_markdown_blocks_javascript_and_protocol_relative_hrefs():
    html_out = render_markdown("[click](javascript:alert(1)) and [ok](https://boxxcoffee.com)")
    assert "javascript:" not in html_out
    assert 'href="https://boxxcoffee.com"' in html_out
    assert "<script>" not in render_markdown("<script>alert(1)</script>")
    # protocol-relative //host is not "relative"
    out = render_markdown("[invoice](//evil.example/phish) [rel](/runs)")
    assert 'href="//evil.example/phish"' not in out
    assert 'href="/runs"' in out


def test_mime_filename_backslash_and_extension_preserved():
    raw = build_mime(
        "hello@boxxcoffee.com", ["x@y.com"], "s", "b",
        attachments=[
            {"filename": "..\\..\\evil.exe", "content_type": "application/pdf", "data": b"x"},
            {"filename": "p" * 300 + ".pdf", "content_type": "application/pdf", "data": b"x"},
        ],
    )
    atts = list(decode_mime(raw).iter_attachments())
    assert atts[0].get_filename() == "evil.exe"  # traversal components stripped
    assert atts[1].get_filename().endswith(".pdf")  # extension survives truncation
    assert len(atts[1].get_filename()) <= 160


def test_login_refuses_default_secret_outside_dev(client, monkeypatch):
    from app.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "session_secret", "dev-secret-change-me", raising=False)
    monkeypatch.setattr(settings, "dev_mode", False, raising=False)
    r = client.post("/login", data={"password": "test-password"})
    assert r.status_code == 503
    assert "SESSION_SECRET" in r.text


def test_forged_default_secret_cookie_rejected_by_gate(client, monkeypatch):
    """The real vulnerability: a cookie signed offline with the KNOWN default
    secret must not pass the auth middleware when the server runs with it."""
    from itsdangerous import URLSafeTimedSerializer

    from app.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "session_secret", "dev-secret-change-me", raising=False)
    monkeypatch.setattr(settings, "dev_mode", False, raising=False)
    forged = URLSafeTimedSerializer("dev-secret-change-me", salt="mise-session").dumps(
        {"v": "mise-authenticated", "n": "attacker"}
    )
    client.cookies.set("mise_session", forged)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


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
