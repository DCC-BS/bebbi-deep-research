"""Chat-turn endpoints used by the pipe: clarifying questions, plan, plan change."""

from typing import Any

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends

from bebbi_deep_research.container import Container
from bebbi_deep_research.models.research import ClarifyRequest, CreatePlanRequest, EditPlanRequest
from bebbi_deep_research.routers.auth import require_token
from bebbi_deep_research.services.research_service import ResearchService

router = APIRouter(tags=["research"], dependencies=[Depends(require_token)])


@router.get("/presets")
@inject
async def list_presets(
    service: ResearchService = Depends(Provide[Container.research_service]),
) -> list[dict[str, str]]:
    """List the research presets."""
    return service.list_presets()


@router.post("/clarify")
@inject
async def clarify(
    request: ClarifyRequest, service: ResearchService = Depends(Provide[Container.research_service])
) -> dict[str, Any]:
    """Return clarifying questions for a task (none if the task is clear)."""
    return await service.clarify(request)


@router.post("/plans")
@inject
async def create_plan(
    request: CreatePlanRequest, service: ResearchService = Depends(Provide[Container.research_service])
) -> dict[str, Any]:
    """Plan a research and store it as a draft."""
    return await service.create_plan(request)


@router.post("/plans/{draft_id}/edit")
@inject
async def edit_plan(
    draft_id: str,
    request: EditPlanRequest,
    service: ResearchService = Depends(Provide[Container.research_service]),
) -> dict[str, Any]:
    """Change a draft plan."""
    return await service.edit_plan(draft_id, request)
