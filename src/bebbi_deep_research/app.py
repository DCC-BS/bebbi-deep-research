"""bebbi-deep-research job service: chat-turn endpoints (clarify, plan, edit) and a job queue with a night window.

Used by the Open WebUI pipe (pipe/deep_research.py). Configuration: environment variables, see
`utils/app_config.py` and docs/operations.md.

    uvicorn bebbi_deep_research.app:create_app --factory --port 8088 --no-access-log
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dcc_backend_common.fastapi_error_handling import ApiErrorCodes, inject_api_error_handler
from dcc_backend_common.fastapi_health_probes import health_probe_router
from dcc_backend_common.fastapi_health_probes.router import ServiceDependency
from dcc_backend_common.fastapi_logging_middleware import add_logging_middleware
from dcc_backend_common.logger import get_logger, init_logger
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bebbi_deep_research.container import Container
from bebbi_deep_research.routers import jobs, monitoring, research
from bebbi_deep_research.services.jobs import describe_error
from bebbi_deep_research.utils.app_config import AppConfig

logger = get_logger(__name__)


def service_dependencies(config: AppConfig) -> list[ServiceDependency]:
    """Return the downstream services the readiness probe checks (the LLM endpoint and Open WebUI, if set)."""
    llm: ServiceDependency = {
        "name": "llm",
        "health_check_url": f"{config.llm_base_url.rstrip('/')}/models",
        "api_key": config.llm_api_key or None,
    }
    deps = [llm]
    if config.owui_url:
        owui: ServiceDependency = {
            "name": "open-webui",
            "health_check_url": f"{config.owui_url}/health",
            "api_key": None,
        }
        deps.append(owui)
    return deps


async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Answer LLM or tool failures with a readable German message (502) instead of a bare 500."""
    logger.error("request failed", path=request.url.path, exc_info=exc)
    message = "Die Anfrage konnte nicht verarbeitet werden. " + describe_error(exc)
    return JSONResponse(
        {"errorId": ApiErrorCodes.SERVICE_UNAVAILABLE, "status": 502, "debugMessage": message}, status_code=502
    )


def create_app(container: Container | None = None) -> FastAPI:
    """Build the FastAPI application.

    Args:
        container: Dependency container; tests pass one with overridden providers.

    Returns:
        The application; its lifespan opens the database and starts the job runner.
    """
    container = container or Container()
    config = container.config()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_logger(app_name="bebbi-deep-research")
        db, runner = container.db(), container.job_runner()
        logger.info("starting", config=str(config))
        await db.init()
        await runner.start()
        yield
        logger.info("shutting down: re-queueing running jobs")
        await runner.stop()
        await db.close()

    app = FastAPI(title="bebbi-deep-research", lifespan=lifespan)
    app.state.container = container
    inject_api_error_handler(app)
    app.add_exception_handler(Exception, unexpected_error)
    add_logging_middleware(app)
    app.include_router(health_probe_router(service_dependencies(config)))
    app.include_router(monitoring.router)
    app.include_router(research.router)
    app.include_router(jobs.router)
    return app
