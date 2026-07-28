from functools import lru_cache

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Database -----------------------------------------------------------
    # Two roles, two privilege levels - see database/session.py for why.
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "percepta"

    # Schema owner. Migrations, seeding and background workers. Bypasses RLS.
    postgres_user: str = "percepta"
    postgres_password: str = Field(default="change-me")

    # Least-privilege role the web/API tier uses (NOSUPERUSER, NOBYPASSRLS), so
    # row-level security actually constrains it. When no password is configured
    # the app falls back to the owner connection and RLS is bypassed - the app
    # still runs, but start-up warns loudly, because that is not a safe state to
    # be in outside local development.
    #
    # Names match DroneOps (APP_DB_USER / APP_DB_PASSWORD) so the two codebases
    # are configured the same way and nothing has to be re-learned between them.
    app_db_user: str = "percepta_app"
    app_db_password: str | None = None

    # PgBouncer in transaction-pooling mode requires psycopg's auto-prepare to
    # be disabled - see session.py. When enabled the app tier connects through
    # the pooler instead of straight to Postgres.
    pgbouncer_enabled: bool = False
    pgbouncer_host: str = "pgbouncer"
    pgbouncer_port: int = 6432

    # --- Auth ---------------------------------------------------------------
    secret_key: str = Field(default="change-me")
    access_token_expire_minutes: int = 60 * 12

    # Fernet key for secrets held at rest (TOTP seeds, and device credentials
    # once enrolment lands). Unset means those columns are stored in plaintext -
    # the app still boots, but core/crypto.warn_if_unencrypted shouts at startup.
    # Back this up separately from the database; storing it beside a dump
    # defeats the control entirely.
    secrets_encryption_key: str | None = None

    # --- Redis --------------------------------------------------------------
    # Control leases, cross-process live fan-out, revocation push, stream-ticket
    # single-use tracking.
    redis_url: str = "redis://localhost:6379/0"

    # --- Real-time ----------------------------------------------------------
    # How often an open WebSocket revalidates its session and station grants,
    # independently of the Redis revocation push. Bounds worst-case staleness if
    # a push is ever missed - see docs/03-realtime-isolation.md section 6.
    stream_revalidate_seconds: int = 60

    # Lifetime of a media stream ticket. Deliberately short: the ticket only has
    # to survive the round trip from issuing it to attaching the stream.
    stream_ticket_seconds: int = 60

    # Cross-worker fan-out and immediate revocation over Redis. Turning this off
    # confines fan-out to a single process, so WEB_CONCURRENCY must then be 1 or
    # subscribers on other workers silently miss frames.
    realtime_bus_enabled: bool = True

    @computed_field
    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field
    @property
    def app_database_url(self) -> str:
        """Least-privilege connection. Falls back to the owner URL when the app
        role has no password configured (see app_db_password).

        Note this deliberately targets PgBouncer's port when pooling is enabled -
        the app tier connects through the pooler, never straight to Postgres.
        """
        if not self.app_db_password:
            return self.database_url
        host = self.pgbouncer_host if self.pgbouncer_enabled else self.postgres_host
        port = self.pgbouncer_port if self.pgbouncer_enabled else self.postgres_port
        return (
            f"postgresql+psycopg://{self.app_db_user}:{self.app_db_password}"
            f"@{host}:{port}/{self.postgres_db}"
        )

    @computed_field
    @property
    def rls_enabled(self) -> bool:
        """False when the app tier is running as the schema owner, which
        bypasses row-level security. Start-up warns on this."""
        return bool(self.app_db_password)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
