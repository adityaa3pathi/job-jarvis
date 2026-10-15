"""Settings, secrets loading and startup safety checks (KTD16)."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC_FOLDER_MARKERS = ("dropbox", "onedrive", "google drive", "googledrive", "icloud", "mega")


class ConfigError(Exception):
    """Raised when startup must be refused."""


MAX_OUTPUT_TOKENS = {
    "jd_extract": 1200, "jd_repair": 1200, "coach_report": 800, "coach_chat": 400,
    "rewrite_bullets": 3000, "profile_digest": 1200,
}  # fmt: skip


class LLMSettings(BaseModel):
    provider: str = "gemini"
    model: str = "gemini-3.5-flash-lite"
    timeout_s: float = 30.0
    send_contact_details: bool = False
    max_output_tokens: dict[str, int] = Field(default_factory=lambda: dict(MAX_OUTPUT_TOKENS))
    thinking_budget: int | None = None  # off: the field name for this model is unconfirmed
    daily_token_budget: int = Field(default=1_000_000, ge=0)  # 0 means no limit
    daily_call_budget: int = Field(default=200, ge=0)  # 0 means no limit


class RankingSettings(BaseModel):
    judge_top_n: int = Field(default=30, ge=0)  # new found jobs the AI judge sees per IST day
    digest_top_n: int = Field(default=10, ge=1, le=50)
    owner_years: int | None = Field(default=None, ge=0)  # unset: no years term in the ceiling
    embed_new_per_run: int = Field(default=300, ge=0)  # model vectors per run; rest wait a run


class Settings(BaseModel):
    daily_cap: int = 5
    per_portal_limit: int = 3
    min_pause_s: float = 20.0
    follow_up_days: int = 7
    retention_days: int = 90
    close_after_clean_runs: int = Field(default=3, ge=1)  # misses before a found job is closed
    detail_fetch_cap: int = Field(default=200, ge=0)  # new jobs' text fetched one by one per run
    years_rounding: Literal["down", "nearest", "up"] = "down"
    fit_bar: int = Field(default=4, ge=1, le=5)
    max_years_asked: int = 5
    freetext_questions: Literal["pause_all", "owner_approved_drafts"] = "pause_all"
    llm: LLMSettings = LLMSettings()
    ranking: RankingSettings = RankingSettings()
    repo_root: Path = REPO_ROOT
    config_dir: Path = Path.home() / ".config" / "jobagent"
    data_dir: Path = REPO_ROOT / "data"

    @property
    def stop_file(self) -> Path:
        return self.config_dir / "STOP"

    @property
    def lock_file(self) -> Path:
        return self.config_dir / "run.lock"

    @property
    def secrets_file(self) -> Path:
        return self.config_dir / "secrets.env"

    @property
    def audit_dir(self) -> Path:
        return self.data_dir / "audit"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobagent.sqlite3"


def load_settings(
    config_path: Path | None = None,
    *,
    config_dir: Path | None = None,
    data_dir: Path | None = None,
) -> Settings:
    """Read config.yaml; paths come from arguments or JOBAGENT_* env, never the cwd."""
    path = config_path or REPO_ROOT / "config.yaml"
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    settings = Settings.model_validate(raw or {})
    env_config = os.environ.get("JOBAGENT_CONFIG_DIR")
    env_data = os.environ.get("JOBAGENT_DATA_DIR")
    chosen_config = config_dir or (Path(env_config) if env_config else None)
    chosen_data = data_dir or (Path(env_data) if env_data else None)
    if chosen_config:
        settings.config_dir = chosen_config.resolve()
    if chosen_data:
        settings.data_dir = chosen_data.resolve()
    return settings


def parse_env_file(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def load_secrets(settings: Settings, *, export: bool = True) -> dict[str, str]:
    """Refuse to start unless the secrets file exists, is mode 0600 and is ours."""
    path = settings.secrets_file
    if not path.is_file():
        raise ConfigError(f"secrets file missing: {path}")
    info = path.stat()
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ConfigError(f"secrets file must be mode 0600: {path}")
    if info.st_uid != os.getuid():
        raise ConfigError(f"secrets file owned by another user: {path}")
    values = parse_env_file(path.read_text())
    if export:
        os.environ.update(values)
    return values


REQUIRED_ENV = ("JOBAGENT_OWNER_HASH", "JOBAGENT_SESSION_SECRET")
PRODUCTION_ENV = ("TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN", "GEMINI_API_KEY")
REMOTE_DB_SCHEMES = ("libsql://", "https://", "http://", "wss://", "ws://")


def is_production(env: Mapping[str, str]) -> bool:
    """Render sets RENDER on every service; JOBAGENT_ENV covers any other host."""
    return bool(env.get("RENDER")) or env.get("JOBAGENT_ENV", "").lower() == "production"


def check_config(env: Mapping[str, str] = os.environ) -> None:
    """Refuse to start with one error naming every missing setting. In production the
    database must be remote: a local file on a free host is wiped on the next restart."""
    needed = [*REQUIRED_ENV, *(PRODUCTION_ENV if is_production(env) else ())]
    missing = [name for name in needed if not env.get(name, "").strip()]
    if missing:
        raise ConfigError(f"missing required settings: {', '.join(missing)}")
    url = env.get("TURSO_DATABASE_URL", "").strip().lower()
    if is_production(env) and not url.startswith(REMOTE_DB_SCHEMES):
        raise ConfigError("TURSO_DATABASE_URL must be a remote database URL (libsql://...)")


def ensure_data_dir(settings: Settings) -> Path:
    """Create data/ as 0700 and refuse a loose or cloud-synced folder."""
    data = settings.data_dir
    lowered = str(data).lower()
    if any(marker in lowered for marker in SYNC_FOLDER_MARKERS):
        raise ConfigError(f"data dir is inside a cloud-synced folder: {data}")
    if not data.exists():
        data.mkdir(parents=True, mode=0o700)
        data.chmod(0o700)
    if stat.S_IMODE(data.stat().st_mode) & 0o077:
        raise ConfigError(f"data dir is group or world accessible: {data}")
    return data
