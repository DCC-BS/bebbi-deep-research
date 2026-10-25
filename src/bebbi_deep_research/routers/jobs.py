"""Job endpoints: submit, status and report, Word file, evidence, cancel."""

from typing import Any

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Response

from bebbi_deep_research.container import Container
from bebbi_deep_research.models.research import SubmitJobRequest
from bebbi_deep_research.routers.auth import require_token
from bebbi_deep_research.routers.contract import require_api_version
from bebbi_deep_research.services.docx_export import MIME
from bebbi_deep_research.services.research_service import ResearchService

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[Depends(require_token), Depends(require_api_version)])


@router.post("")
@inject
async def submit_job(
    request: SubmitJobRequest, service: ResearchService = Depends(Provide[Container.research_service])
) -> dict[str, Any]:
    """Queue the research of an approved draft."""
    return await service.submit(request)


@router.get("/lookup")
@inject
async def lookup_job(
    chat_id: str,
    message_id: str,
    user_id: str | None = None,
    service: ResearchService = Depends(Provide[Container.research_service]),
) -> dict[str, str]:
    """Return the newest job that delivers into a chat message."""
    return await service.lookup(chat_id, message_id, user_id)


@router.get("/{job_id}")
@inject
async def get_job(
    job_id: str,
    user_id: str | None = None,
    service: ResearchService = Depends(Provide[Container.research_service]),
) -> dict[str, Any]:
    """Return status and progress of a job, and the report with its sources when it is done."""
    return await service.job_view(job_id, user_id)


@router.get("/{job_id}/report.docx")
@inject
async def get_word_file(
    job_id: str,
    user_id: str | None = None,
    service: ResearchService = Depends(Provide[Container.research_service]),
) -> Response:
    """Return the finished report as Word file."""
    word = await service.word_file(job_id, user_id)
    return Response(
        word.data, media_type=MIME, headers={"Content-Disposition": f'attachment; filename="{word.filename}"'}
    )


@router.post("/{job_id}/cancel")
@inject
async def cancel_job(
    job_id: str,
    user_id: str | None = None,
    service: ResearchService = Depends(Provide[Container.research_service]),
) -> dict[str, str]:
    """Cancel a queued or running job (409 if it has already ended)."""
    return await service.cancel(job_id, user_id)


@router.get("/{job_id}/evidence")
@inject
async def get_evidence(
    job_id: str,
    user_id: str | None = None,
    service: ResearchService = Depends(Provide[Container.research_service]),
) -> dict[str, Any]:
    """Return the evidence ledger of a job."""
    return await service.evidence(job_id, user_id)
