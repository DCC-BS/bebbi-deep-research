"""Prometheus metrics (unauthenticated, like the health probes)."""

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Response

from bebbi_deep_research.container import Container
from bebbi_deep_research.services.research_service import ResearchService

router = APIRouter(tags=["monitoring"])


@router.get("/metrics")
@inject
async def metrics(service: ResearchService = Depends(Provide[Container.research_service])) -> Response:
    """Return job counts per status and whether the night window is open."""
    return Response(await service.metrics(), media_type="text/plain; version=0.0.4")
