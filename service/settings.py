"""Service configuration, read from environment variables (see docs/operations.md for the reference)."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent


def _json_env(env: dict, name: str) -> dict:
    raw = (env.get(name) or "").strip()
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


@dataclass
class Settings:
    token: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    model_think: str = "gemma-think"
    model_fast: str = "gemma-fast"
    model_embed: str = "qwen-embed"
    extra_body_think: dict = field(default_factory=dict)
    extra_body_fast: dict = field(default_factory=dict)
    search_url: str = ""
    search_key: str = ""
    firecrawl_url: str = ""
    firecrawl_key: str = ""
    presets_path: Path = ROOT / "config" / "presets.yaml"
    database_url: str = "sqlite+aiosqlite:////tmp/research-agent.db"
    night_window: str = "20:00-05:00"
    timezone: str = "Europe/Zurich"
    workers: int = 1
    day_workers: int = 1
    quota_per_day: int = 0
    poll_seconds: float = 5.0
    stale_seconds: float = 120.0
    owui_url: str = ""
    owui_api_key: str = ""
    log_level: str = "INFO"
    own_max_files: int = 10                 # uploaded files per research (own sources)
    own_max_file_chars: int = 2_000_000     # extracted text per file; longer texts are cut
    own_max_collections: int = 5            # knowledge bases per research
    parse_errors: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        env = os.environ if env is None else env
        errors: list[str] = []

        def num(name: str, default, cast=int):
            try:
                return cast(env.get(name, default))
            except ValueError:
                errors.append(f"{name} is not a number")
                return default

        def js(name: str) -> dict:
            try:
                return _json_env(env, name)
            except ValueError as e:
                errors.append(f"{name}: {e}")
                return {}

        db = env.get("RA_DATABASE_URL") or (f"sqlite+aiosqlite:///{env['RA_DB']}" if env.get("RA_DB") else cls.database_url)
        s = cls(
            token=env.get("RA_TOKEN", ""),
            llm_base_url=env.get("RA_LLM_BASE_URL", ""), llm_api_key=env.get("RA_LLM_API_KEY", ""),
            model_think=env.get("RA_LLM_MODEL_THINK", cls.model_think),
            model_fast=env.get("RA_LLM_MODEL_FAST", cls.model_fast),
            model_embed=env.get("RA_LLM_MODEL_EMBED", cls.model_embed),
            extra_body_think=js("RA_LLM_EXTRA_BODY_THINK"), extra_body_fast=js("RA_LLM_EXTRA_BODY_FAST"),
            search_url=env.get("RA_SEARCH_URL", ""), search_key=env.get("RA_SEARCH_KEY", ""),
            firecrawl_url=env.get("RA_FIRECRAWL_URL", "").rstrip("/"), firecrawl_key=env.get("RA_FIRECRAWL_KEY", ""),
            presets_path=Path(env.get("RA_PRESETS") or cls.presets_path),
            database_url=normalize_db_url(db),
            night_window=env.get("RA_NIGHT_WINDOW", cls.night_window), timezone=env.get("RA_TIMEZONE", cls.timezone),
            workers=num("RA_WORKERS", 1), day_workers=num("RA_DAY_WORKERS", 1), quota_per_day=num("RA_QUOTA_PER_DAY", 0),
            poll_seconds=num("RA_POLL_SECONDS", 5.0, float), stale_seconds=num("RA_STALE_SECONDS", 120.0, float),
            owui_url=env.get("OWUI_URL", "").rstrip("/"), owui_api_key=env.get("OWUI_API_KEY", ""),
            log_level=env.get("RA_LOG_LEVEL", "INFO").upper(),
            own_max_files=num("RA_OWN_MAX_FILES", 10), own_max_file_chars=num("RA_OWN_MAX_FILE_CHARS", 2_000_000),
            own_max_collections=num("RA_OWN_MAX_COLLECTIONS", 5),
        )
        s.parse_errors = errors
        return s

    def problems(self) -> list[str]:
        """Configuration errors that make the service unusable (reported by /readyz)."""
        out = list(self.parse_errors)
        for name, value in (("RA_TOKEN", self.token), ("RA_LLM_BASE_URL", self.llm_base_url),
                            ("RA_SEARCH_URL", self.search_url), ("RA_FIRECRAWL_URL", self.firecrawl_url)):
            if not value:
                out.append(f"{name} is not set")
        try:
            self.window()
        except Exception:
            out.append("RA_NIGHT_WINDOW must look like 20:00-05:00")
        try:
            ZoneInfo(self.timezone)
        except Exception:
            out.append(f"RA_TIMEZONE {self.timezone!r} is unknown")
        if not self.presets_path.is_file():
            out.append(f"RA_PRESETS {self.presets_path} not found")
        return out

    def window(self):
        from datetime import datetime
        start, end = (datetime.strptime(x.strip(), "%H:%M").time() for x in self.night_window.split("-"))
        return start, end


def normalize_db_url(url: str) -> str:
    """postgresql://... and sqlite:///... are accepted and mapped to the async drivers."""
    if url.startswith(("postgresql://", "postgres://")):
        return "postgresql+asyncpg://" + url.split("://", 1)[1]
    if url.startswith("sqlite://") and "+aiosqlite" not in url:
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return url
