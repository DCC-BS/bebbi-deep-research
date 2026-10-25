"""Job policy shared by the API, the research service and the job runner.

German error text for the chat, the night window, the daytime rule and job/draft ids.
"""

import re
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import openai
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UnexpectedModelBehavior

from bebbi_deep_research.engine.engines import engine_class
from bebbi_deep_research.utils.app_config import AppConfig

_HOSTS = re.compile(
    r"https?://\S+|\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b|\b(?:[\w-]+\.)+[A-Za-z]{2,}(?::\d+)?\b|\blocalhost(?::\d+)?\b"
)


def describe_error(e: BaseException) -> str:
    """Return a German, user-facing reason for a failure (shown in the chat), followed by a short technical detail.

    URLs, IP addresses and host names are removed from the detail.
    """
    detail = _HOSTS.sub("<host>", " ".join(str(getattr(e, "message", None) or e).split()))[:200]
    rejected = isinstance(e, ModelHTTPError) and 400 <= e.status_code < 500 and e.status_code not in (408, 429)
    if rejected or isinstance(
        e, (openai.NotFoundError, openai.BadRequestError, openai.AuthenticationError, openai.PermissionDeniedError)
    ):
        reason = "Das Sprachmodell lehnt die Anfrage ab (Modellname oder Zugang falsch konfiguriert)."
    elif isinstance(e, (ModelAPIError, openai.APIConnectionError, openai.APITimeoutError)):
        reason = "Das Sprachmodell ist nicht erreichbar."
    elif isinstance(e, UnexpectedModelBehavior):
        reason = "Das Sprachmodell hat keine verwertbare Antwort geliefert."
    elif isinstance(e, TimeoutError):
        reason = "Die Recherche hat das Zeitlimit überschritten."
    elif isinstance(e, httpx.HTTPError):
        reason = "Ein Dienst der Recherche (Suche oder Seitenabruf) ist nicht erreichbar."
    else:
        reason = "Unerwarteter Fehler."
    return (
        f"{reason} Bitte versuchen Sie es später erneut oder wenden Sie sich an den Betrieb. "
        f"(Technisch: {type(e).__name__}: {detail})"
    )


def in_window(settings: AppConfig, now: datetime | None = None) -> bool:
    """Return whether `now` (default: the current time in the configured zone) is inside the night window."""
    start, end = settings.window()
    t = (now or datetime.now(ZoneInfo(settings.timezone))).time()
    return (t >= start or t < end) if start > end else start <= t < end


def runs_by_day(cfg, preset: str, depth: str) -> bool:
    """Office hours: only quick runs of an engine that allows it (focus: about 20-50k tokens, 2-3 min) start right away."""
    p = cfg.presets.get(preset)
    return depth == "quick" and p is not None and engine_class(p.engine).runs_by_day


def new_id() -> str:
    """Return a new 12-character job or draft id."""
    return uuid.uuid4().hex[:12]
