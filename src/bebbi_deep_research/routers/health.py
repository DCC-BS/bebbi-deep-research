"""Health probes (unauthenticated): liveness, startup, readiness (database only) and the downstream status.

Readiness checks only what every request needs: the database (job status and reports come from it). The LLM endpoint
and Open WebUI are reported by `GET /health/dependencies`, which never fails the probe: a vLLM restart at night must
not cut the pipe off job status and reports, and running jobs handle an LLM outage themselves.
"""

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from dcc_backend_common.logger import get_logger
from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Response

from bebbi_deep_research.container import Container
from bebbi_deep_research.services.db import DB
from bebbi_deep_research.utils.app_config import AppConfig

logger = get_logger("health")
router = APIRouter(prefix="/health", tags=["health"])
START = time.time()
DB_TIMEOUT = 3.0  # below the probe timeout of the chart (8 s)
DEPENDENCY_TIMEOUT = 5.0
_db_failing: list[bool] = [False]  # log a database outage once, and its recovery


@router.get("/liveness")
async def liveness() -> dict[str, Any]:
    """The process is up (Kubernetes restarts the container if not)."""
    return {"status": "up", "uptime_seconds": time.time() - START}


@router.get("/startup")
async def startup() -> dict[str, Any]:
    """The application has started (the lifespan created the tables and started the worker)."""
    return {"status": "started", "timestamp": datetime.now(UTC).isoformat()}


@router.get("/readiness")
@inject
async def readiness(response: Response, db: DB = Depends(Provide[Container.db])) -> dict[str, Any]:
    """200 if the database answers within 3 s, else 503 (the pod gets no traffic)."""
    try:
        await asyncio.wait_for(db.ping(), DB_TIMEOUT)
    except Exception as e:
        detail = "timeout" if isinstance(e, TimeoutError) else type(e).__name__
        if not _db_failing[0]:
            logger.error("health check failed", service="database", reason=detail, detail=repr(e)[:200])
        _db_failing[0] = True
        response.status_code = 503
        return {"status": "unhealthy", "checks": {"database": detail}}
    if _db_failing[0]:
        logger.info("health check recovered", service="database")
    _db_failing[0] = False
    return {"status": "ready", "checks": {"database": "healthy"}}


async def _check(http: httpx.AsyncClient, url: str, api_key: str = "") -> str:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        r = await http.get(url, headers=headers)
    except httpx.TimeoutException:
        return "timeout"
    except httpx.HTTPError as e:
        return f"unreachable ({type(e).__name__})"
    return "healthy" if r.status_code == 200 else f"status {r.status_code}"


@router.get("/dependencies")
@inject
async def dependencies(config: AppConfig = Depends(Provide[Container.config])) -> dict[str, Any]:
    """Status of the LLM endpoint (`/models`) and Open WebUI (`/health`, if `OWUI_URL` is set); always 200."""
    targets = {"llm": (f"{config.llm_base_url.rstrip('/')}/models", config.llm_api_key)}
    if config.owui_url:
        targets["open-webui"] = (f"{config.owui_url}/health", "")
    async with httpx.AsyncClient(timeout=DEPENDENCY_TIMEOUT) as http:
        results = await asyncio.gather(*(_check(http, url, key) for url, key in targets.values()))
    checks = dict(zip(targets, results, strict=True))
    return {"status": "ok" if all(v == "healthy" for v in checks.values()) else "degraded", "checks": checks}
