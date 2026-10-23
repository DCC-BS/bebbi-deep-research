"""Service configuration, read from environment variables (reference: docs/operations.md, schema: .env.schema)."""

import json
import os
from collections.abc import Mapping
from datetime import datetime, time
from pathlib import Path
from typing import Any, Self, override
from zoneinfo import ZoneInfo

from dcc_backend_common.config import AbstractAppConfig, AppConfigError, log_secret
from pydantic import Field, model_validator

ROOT = Path(__file__).resolve().parents[3]  # repository root (config/presets.yaml); RA_PRESETS overrides it
DEFAULT_PRESETS = ROOT / "config" / "presets.yaml"
DELIVERY_PATH = "/api/v1/dcc/deep-research/deliver"  # route added by the Open WebUI image (kdkp dr_delivery hook)
DEFAULT_DATABASE_URL = "sqlite+aiosqlite:////tmp/research-agent.db"


def normalize_db_url(url: str) -> str:
    """Map plain database URLs to the async drivers.

    Args:
        url: A ``postgresql://``, ``postgres://`` or ``sqlite://`` URL, or one that already names a driver.

    Returns:
        The URL with ``postgresql+asyncpg`` or ``sqlite+aiosqlite`` as scheme.
    """
    if url.startswith(("postgresql://", "postgres://")):
        return "postgresql+asyncpg://" + url.split("://", 1)[1]
    if url.startswith("sqlite://") and "+aiosqlite" not in url:
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return url


def _required(env: Mapping[str, str], name: str) -> str:
    """Return a required, non-empty variable.

    Raises:
        AppConfigError: If the variable is missing or empty.
    """
    value = env.get(name, "")
    if not value:
        raise AppConfigError(name)
    return value


def _json_object(env: Mapping[str, str], name: str) -> dict[str, Any]:
    """Parse an optional JSON object variable (empty means ``{}``).

    Raises:
        AppConfigError: If the value is not a JSON object.
    """
    raw = (env.get(name) or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError as e:
        raise AppConfigError(name) from e
    if not isinstance(value, dict):
        raise AppConfigError(name)
    return value


class AppConfig(AbstractAppConfig):
    """Configuration of the job service.

    Attributes:
        token: Bearer token of the API (``RA_TOKEN``); whoever holds it has full access.
        hmac_secret: Secret for pseudonymised user ids in usage events (``HMAC_SECRET``).
        llm_base_url: OpenAI-compatible endpoint of the language models.
        night_window: ``HH:MM-HH:MM`` in ``timezone``; heavy jobs run only inside it.
    """

    token: str = Field(description="Bearer token of the API (RA_TOKEN)")
    hmac_secret: str = Field(description="Secret for pseudonymised user ids in usage events (HMAC_SECRET)")
    llm_base_url: str = Field(description="OpenAI-compatible LLM endpoint (RA_LLM_BASE_URL)")
    llm_api_key: str = Field(default="", description="LLM API key; empty for endpoints without auth")
    llm_embed_base_url: str = Field(default="", description="Embeddings endpoint if not RA_LLM_BASE_URL")
    llm_embed_api_key: str = Field(default="", description="API key of the embeddings endpoint")
    model_think: str = "gemma-think"
    model_fast: str = "gemma-fast"
    model_embed: str = "qwen-embed"
    extra_body_think: dict[str, Any] = Field(default_factory=dict)
    extra_body_fast: dict[str, Any] = Field(default_factory=dict)
    search_url: str = Field(description="Serper-compatible search endpoint (RA_SEARCH_URL)")
    search_key: str = ""
    firecrawl_url: str = Field(description="Firecrawl base URL (RA_FIRECRAWL_URL)")
    firecrawl_key: str = ""
    presets_path: Path = DEFAULT_PRESETS
    database_url: str = DEFAULT_DATABASE_URL
    night_window: str = "20:00-05:00"
    timezone: str = "Europe/Zurich"
    workers: int = Field(default=1, ge=1)
    day_workers: int = Field(default=1, ge=0)
    quota_per_day: int = Field(default=0, ge=0)
    poll_seconds: float = Field(default=5.0, gt=0)
    stale_seconds: float = Field(default=120.0, gt=0)
    owui_url: str = ""
    owui_api_key: str = ""
    owui_delivery_url: str = ""
    owui_delivery_secret: str = ""
    own_max_files: int = Field(default=10, ge=0, description="Uploaded files per research")
    own_max_file_chars: int = Field(default=2_000_000, ge=1, description="Extracted text per file; longer is cut")
    own_max_collections: int = Field(default=5, ge=0, description="Knowledge bases per research")

    @model_validator(mode="after")
    def _check(self) -> Self:
        """Reject values that make the service unusable."""
        start, end = self.window()
        if start == end:
            raise ValueError("RA_NIGHT_WINDOW start and end must differ (an empty window never opens)")
        try:
            ZoneInfo(self.timezone)
        except (ValueError, KeyError) as e:
            raise ValueError(f"RA_TIMEZONE {self.timezone!r} is unknown") from e
        if not self.presets_path.is_file():
            raise ValueError(f"RA_PRESETS {self.presets_path} not found")
        return self

    def window(self) -> tuple[time, time]:
        """Return start and end of the night window.

        Raises:
            ValueError: If ``night_window`` does not look like ``20:00-05:00``.
        """
        parts = self.night_window.split("-")
        if len(parts) != 2:
            raise ValueError("RA_NIGHT_WINDOW must look like 20:00-05:00")
        start, end = (datetime.strptime(x.strip(), "%H:%M").time() for x in parts)
        return start, end

    @classmethod
    @override
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AppConfig":
        """Load the configuration from environment variables.

        Args:
            env: Variables to read instead of ``os.environ`` (tests).

        Raises:
            AppConfigError: If a required variable is missing or a JSON variable is invalid.
            pydantic.ValidationError: If a value is out of range, or the night window, time zone or presets file
                is invalid.
        """
        env = os.environ if env is None else env
        db = env.get("RA_DATABASE_URL") or (f"sqlite+aiosqlite:///{env['RA_DB']}" if env.get("RA_DB") else "")
        optional = {
            "model_think": env.get("RA_LLM_MODEL_THINK"),
            "model_fast": env.get("RA_LLM_MODEL_FAST"),
            "model_embed": env.get("RA_LLM_MODEL_EMBED"),
            "presets_path": env.get("RA_PRESETS"),
            "database_url": normalize_db_url(db) if db else None,
            "night_window": env.get("RA_NIGHT_WINDOW"),
            "timezone": env.get("RA_TIMEZONE"),
            "workers": env.get("RA_WORKERS"),
            "day_workers": env.get("RA_DAY_WORKERS"),
            "quota_per_day": env.get("RA_QUOTA_PER_DAY"),
            "poll_seconds": env.get("RA_POLL_SECONDS"),
            "stale_seconds": env.get("RA_STALE_SECONDS"),
            "own_max_files": env.get("RA_OWN_MAX_FILES"),
            "own_max_file_chars": env.get("RA_OWN_MAX_FILE_CHARS"),
            "own_max_collections": env.get("RA_OWN_MAX_COLLECTIONS"),
        }
        return cls.model_validate(
            {
                "token": _required(env, "RA_TOKEN"),
                "hmac_secret": _required(env, "HMAC_SECRET"),
                "llm_base_url": _required(env, "RA_LLM_BASE_URL"),
                "llm_api_key": env.get("RA_LLM_API_KEY", ""),
                "llm_embed_base_url": env.get("RA_LLM_EMBED_BASE_URL", ""),
                "llm_embed_api_key": env.get("RA_LLM_EMBED_API_KEY", ""),
                "extra_body_think": _json_object(env, "RA_LLM_EXTRA_BODY_THINK"),
                "extra_body_fast": _json_object(env, "RA_LLM_EXTRA_BODY_FAST"),
                "search_url": _required(env, "RA_SEARCH_URL"),
                "search_key": env.get("RA_SEARCH_KEY", ""),
                "firecrawl_url": _required(env, "RA_FIRECRAWL_URL").rstrip("/"),
                "firecrawl_key": env.get("RA_FIRECRAWL_KEY", ""),
                "owui_url": env.get("OWUI_URL", "").rstrip("/"),
                "owui_api_key": env.get("OWUI_API_KEY", ""),
                "owui_delivery_url": (
                    env.get("OWUI_DELIVERY_URL", "").rstrip("/")
                    or (f"{env.get('OWUI_URL', '').rstrip('/')}{DELIVERY_PATH}" if env.get("OWUI_URL") else "")
                ),
                "owui_delivery_secret": env.get("OWUI_DELIVERY_SECRET", ""),
                **{k: v for k, v in optional.items() if v},
            }
        )

    @override
    def __str__(self) -> str:
        return (
            f"AppConfig(token={log_secret(self.token)}, hmac_secret={log_secret(self.hmac_secret)}, "
            f"llm_base_url={self.llm_base_url}, llm_api_key={log_secret(self.llm_api_key)}, "
            f"llm_embed_base_url={self.llm_embed_base_url}, llm_embed_api_key={log_secret(self.llm_embed_api_key)}, "
            f"models={self.model_think}/{self.model_fast}/{self.model_embed}, search_url={self.search_url}, "
            f"firecrawl_url={self.firecrawl_url}, presets_path={self.presets_path}, "
            f"database={self.database_url.split('://')[0]}, night_window={self.night_window}, "
            f"timezone={self.timezone}, "
            f"workers={self.workers}, day_workers={self.day_workers}, quota_per_day={self.quota_per_day}, "
            f"owui_url={self.owui_url}, owui_api_key={log_secret(self.owui_api_key)}, "
            f"owui_delivery_url={self.owui_delivery_url}, owui_delivery_secret={log_secret(self.owui_delivery_secret)})"
        )
