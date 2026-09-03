import os

import pytest
from fastapi.testclient import TestClient

os.environ.update(
    APP_PASSWORD="test-password",
    SESSION_SECRET="test-secret",
    DEV_MODE="1",
)

from app.main import app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app)


def test_health_is_public(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["db"] == "absent"


def test_unauthenticated_is_redirected_to_login(client):
    for path in ("/", "/runs", "/pipeline", "/board", "/drafts", "/settings"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/login"


def test_login_page_renders(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "BOXX" in r.text


def test_wrong_password_rejected(client):
    r = client.post("/login", data={"password": "nope"})
    assert r.status_code == 401
    assert "Wrong password" in r.text
    assert "mise_session" not in client.cookies


def test_correct_password_grants_session(client):
    r = client.post("/login", data={"password": "test-password"}, follow_redirects=False)
    assert r.status_code == 303
    assert "mise_session" in client.cookies

    home = client.get("/")
    assert home.status_code == 200
    assert "Good morning, Arda." in home.text


def test_logout_clears_session(client):
    client.post("/login", data={"password": "test-password"})
    client.post("/logout")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303


def test_tampered_cookie_rejected(client):
    client.cookies.set("mise_session", "forged-token")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303


def test_settings_page_without_db(client):
    client.post("/login", data={"password": "test-password"})
    r = client.get("/settings")
    assert r.status_code == 200
    assert "No routines" in r.text  # degrades gracefully with no DATABASE_URL


def test_run_now_without_db_reports_error(client):
    client.post("/login", data={"password": "test-password"})
    r = client.post("/routines/1/run", follow_redirects=False)
    assert r.status_code == 303
    from urllib.parse import unquote

    assert "scheduler is not running" in unquote(r.headers["location"])
