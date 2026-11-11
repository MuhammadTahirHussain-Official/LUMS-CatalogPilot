"""Runtime configuration. Every value can be overridden with a CP_-prefixed environment variable."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CP_", env_file=".env", extra="ignore")

    env: str = "development"  # development | test | production
    data_dir: Path = REPO_ROOT / "var"
    database_url: str = ""  # empty -> sqlite file inside data_dir

    # Sessions (SEC-SES-01/03)
    session_idle_minutes: int = 30
    session_absolute_hours: int = 12
    step_up_minutes: int = 5
    cookie_secure: bool = False  # must be True in production (enforced in validate())
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # Login throttling (SEC-IAM-09)
    login_max_failures: int = 5
    login_lock_minutes: int = 15

    # Upload limits (SRS 4.1), deliberately smaller defaults for a single-node install
    max_file_bytes: int = 500 * 1024 * 1024
    max_rows: int = 1_000_000
    max_columns: int = 1_000
    max_cell_bytes: int = 32 * 1024
    zip_max_entries: int = 50_000
    zip_max_uncompressed: int = 20 * 1024**3
    zip_max_ratio: int = 100
    upload_url_ttl_minutes: int = 15

    # Engine (02_System_Architecture 4.1)
    lease_seconds: int = 30
    heartbeat_seconds: int = 10
    retry_base_seconds: float = 2.0
    retry_cap_seconds: float = 600.0
    retry_max_attempts: int = 8
    poison_threshold: int = 3
    breaker_window: int = 20
    breaker_error_ratio: float = 0.5
    breaker_probe_seconds: float = 60.0
    tenant_pool_share: float = 0.25

    # Vault
    credential_lease_seconds: int = 900
    master_key_b64: str = ""  # empty -> dev key file under data_dir (never in production)

    # AI
    ai_provider: str = "rules"  # rules | anthropic
    anthropic_api_key: str = ""
    ai_model: str = "claude-haiku-5-5"
    ai_job_token_budget: int = 2_000_000

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(self.data_dir / 'catalogpilot.db').as_posix()}"

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    def validate_for_production(self) -> None:
        """Fail closed: refuse to boot production with development-grade settings."""
        if not self.is_production:
            return
        problems = []
        if not self.cookie_secure:
            problems.append("CP_COOKIE_SECURE must be true")
        if not self.master_key_b64:
            problems.append("CP_MASTER_KEY_B64 must be set (use a KMS-backed value)")
        if self.db_url.startswith("sqlite"):
            problems.append("CP_DATABASE_URL must point at PostgreSQL")
        if problems:
            raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()
