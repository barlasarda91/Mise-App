"""Application settings, read from environment variables (Railway service vars)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_password: str | None = None
    session_secret: str = "dev-secret-change-me"
    database_url: str | None = None
    default_tz: str = "America/Los_Angeles"

    # Google Workspace service account (domain-wide delegation): raw JSON or a file path.
    google_sa_json: str | None = None
    gmail_arda_address: str = "ardabarlas@boxxcoffee.com"  # canonical (spec §5)
    gmail_hello_address: str = "hello@boxxcoffee.com"
    # Reminders land on arda's calendar; override only if that ever changes.
    calendar_address: str | None = None

    anthropic_api_key: str | None = None

    # QuickBooks Online (Intuit OAuth2 — separate from the Google SA).
    # QBO_REFRESH_TOKEN is a bootstrap seed only: Intuit rotates refresh
    # tokens on use, and the live token persists in app_state (spec §5).
    qbo_client_id: str | None = None
    qbo_client_secret: str | None = None
    qbo_refresh_token: str | None = None
    qbo_realm_id: str | None = None
    qbo_environment: str = "production"  # or "sandbox"
    # DEV_MODE=1 relaxes the Secure flag on the session cookie for local http.
    dev_mode: bool = False

    session_cookie_name: str = "mise_session"
    session_max_age_seconds: int = 60 * 60 * 24 * 30  # 30 days

    @property
    def sqlalchemy_url(self) -> str | None:
        """Railway hands out postgres:// URLs; SQLAlchemy + psycopg3 wants postgresql+psycopg://."""
        url = self.database_url
        if not url:
            return None
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+psycopg://", 1)
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+psycopg://", 1)
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
