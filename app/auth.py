"""Single-user auth gate: password login -> signed, timestamped session cookie."""

import hmac
import secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.requests import Request

from app.settings import Settings

_SESSION_VALUE = "mise-authenticated"
_DEFAULT_SECRET = "dev-secret-change-me"


def secret_usable(settings: Settings) -> bool:
    """The known default secret must never validate or mint sessions outside
    dev mode — otherwise anyone can forge the cookie offline."""
    return settings.dev_mode or settings.session_secret != _DEFAULT_SECRET


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt="mise-session")


def verify_password(settings: Settings, candidate: str) -> bool:
    if not settings.app_password:
        # No password configured -> the gate stays closed, never open.
        return False
    return hmac.compare_digest(candidate.encode(), settings.app_password.encode())


def issue_session_token(settings: Settings) -> str:
    if not secret_usable(settings):
        raise RuntimeError("SESSION_SECRET is not configured")
    # A random nonce keeps tokens unique; validity comes from the signature + age.
    return _serializer(settings).dumps({"v": _SESSION_VALUE, "n": secrets.token_hex(8)})


def session_is_valid(settings: Settings, token: str | None) -> bool:
    if not token or not secret_usable(settings):
        return False
    try:
        data = _serializer(settings).loads(token, max_age=settings.session_max_age_seconds)
    except (BadSignature, SignatureExpired):
        return False
    return isinstance(data, dict) and data.get("v") == _SESSION_VALUE


def request_is_authenticated(request: Request, settings: Settings) -> bool:
    return session_is_valid(settings, request.cookies.get(settings.session_cookie_name))
