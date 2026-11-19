"""Runtime configuration, read only from environment variables (or a local, gitignored .env).

Secrets are typed as ``SecretStr`` so they render as ``**********`` in logs, reprs and tracebacks.
"""

from __future__ import annotations

from functools import lru_cache

import certifi
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", None, ""}
SECURE_SSLMODES = {"require", "verify-ca", "verify-full"}


def check_database_url(url: str) -> str:
    """Reject remote database URLs that don't force TLS.

    Local development databases (localhost / unix socket) are exempt.
    """
    parsed = make_url(url)
    if not parsed.drivername.startswith("postgresql"):
        raise ValueError("database URL must be a postgresql URL")
    if parsed.host not in LOCAL_HOSTS:
        sslmode = parsed.query.get("sslmode")
        if sslmode not in SECURE_SSLMODES:
            raise ValueError(
                "remote database URLs must set sslmode=verify-full (preferred) or require"
            )
    return url


def resolve_ssl_root_cert(url: str) -> str:
    """Make ``sslrootcert=system`` work everywhere.

    psycopg's binary wheel bundles its own OpenSSL, which can't find the OS trust store (on macOS
    it fails with "certificate verify failed"). Replace ``system`` with certifi's maintained CA
    bundle so ``sslmode=verify-full`` behaves the same on a laptop and in GitHub Actions.
    """
    parsed = make_url(url)
    if parsed.query.get("sslrootcert") != "system":
        return url
    return parsed.update_query_dict({"sslrootcert": certifi.where()}).render_as_string(
        hide_password=False
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FT_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: SecretStr
    migration_database_url: SecretStr | None = None

    travelpayouts_token: SecretStr | None = None
    serpapi_key: SecretStr | None = None

    resend_api_key: SecretStr | None = None
    alert_email_from: str = "Flight Tracker <onboarding@resend.dev>"

    # Budgets. SerpApi's free plan is 250 searches/month; keep a safety margin.
    serpapi_monthly_limit: int = Field(default=240, ge=1, le=100_000)
    serpapi_max_per_run: int = Field(default=1, ge=0, le=50)
    # Cap on Travelpayouts requests per scan run (one request per watch per departure month).
    travelpayouts_max_per_run: int = Field(default=60, ge=0, le=1_000)

    user_agent: str = "flight-deal-tracker/0.1 (personal, non-commercial)"
    http_timeout_seconds: float = Field(default=20.0, gt=0, le=120)

    @field_validator("database_url", "migration_database_url")
    @classmethod
    def _secure_db_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and value.get_secret_value():
            check_database_url(value.get_secret_value())
        return value

    @property
    def effective_migration_url(self) -> str:
        if self.migration_database_url and self.migration_database_url.get_secret_value():
            return self.migration_database_url.get_secret_value()
        return self.database_url.get_secret_value()


class MigrationSettings(BaseSettings):
    """Just the database URLs, read the same way as ``Settings`` (environment, then .env), so
    migrations don't require API keys or other job settings to be present."""

    model_config = SettingsConfigDict(
        env_prefix="FT_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: SecretStr | None = None
    migration_database_url: SecretStr | None = None

    def url(self) -> str | None:
        for value in (self.migration_database_url, self.database_url):
            if value is not None and value.get_secret_value():
                return value.get_secret_value()
        return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # values come from the environment
