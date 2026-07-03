import os
from dataclasses import dataclass, field


def _bool(name: str) -> bool:
    return os.environ.get(name, "0").strip() in ("1", "true", "yes")


@dataclass
class Settings:
    devin_api_key: str = field(default_factory=lambda: os.environ.get("DEVIN_API_KEY", ""))
    devin_org_id: str = field(default_factory=lambda: os.environ.get("DEVIN_ORG_ID", ""))
    devin_api_base: str = field(
        default_factory=lambda: os.environ.get("DEVIN_API_BASE", "https://api.devin.ai/v3")
    )

    github_token: str = field(default_factory=lambda: os.environ.get("GITHUB_TOKEN", ""))
    github_repo: str = field(default_factory=lambda: os.environ.get("GITHUB_REPO", ""))
    webhook_secret: str = field(
        default_factory=lambda: os.environ.get("GITHUB_WEBHOOK_SECRET", "change-me")
    )

    mock_mode: bool = field(default_factory=lambda: _bool("MOCK_MODE"))
    kill_switch: bool = field(default_factory=lambda: _bool("KILL_SWITCH"))

    max_concurrent_sessions: int = field(
        default_factory=lambda: int(os.environ.get("MAX_CONCURRENT_SESSIONS", "3"))
    )
    max_acu_per_session: int = field(
        default_factory=lambda: int(os.environ.get("MAX_ACU_PER_SESSION", "8"))
    )
    daily_session_budget: int = field(
        default_factory=lambda: int(os.environ.get("DAILY_SESSION_BUDGET", "15"))
    )
    poll_interval_seconds: int = field(
        default_factory=lambda: int(os.environ.get("POLL_INTERVAL_SECONDS", "20"))
    )
    dispatch_interval_seconds: int = field(
        default_factory=lambda: int(os.environ.get("DISPATCH_INTERVAL_SECONDS", "10"))
    )
    stale_after_minutes: int = field(
        default_factory=lambda: int(os.environ.get("STALE_AFTER_MINUTES", "45"))
    )

    snapshot_id: str = field(default_factory=lambda: os.environ.get("SNAPSHOT_ID", ""))
    playbook_id: str = field(default_factory=lambda: os.environ.get("PLAYBOOK_ID", ""))

    db_path: str = field(default_factory=lambda: os.environ.get("DB_PATH", "data/fleet.sqlite3"))


settings = Settings()
