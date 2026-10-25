"""Chat-turn and job logic behind the API: clarify, plan, edit, submit, job views and the Word file."""

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from dcc_backend_common.logger import get_logger
from dcc_backend_common.usage_tracking import UsageTrackingService

from bebbi_deep_research.engine.config import Config
from bebbi_deep_research.models.own_sources import OwnSourcesRequest
from bebbi_deep_research.models.research import (
    ClarifyRequest,
    CreatePlanRequest,
    EditPlanRequest,
    RunWhen,
    SubmitJobRequest,
)
from bebbi_deep_research.services.db import DB
from bebbi_deep_research.services.docx_export import docx_filename, render_docx
from bebbi_deep_research.services.own_sources import describe, prepare, to_engine
from bebbi_deep_research.services.policy import in_window, new_id, runs_by_day
from bebbi_deep_research.services.report import build_sources, owui_source_events, ui_markdown
from bebbi_deep_research.utils.app_config import AppConfig
from bebbi_deep_research.utils.errors import conflict, invalid_request, not_found, rate_limited, user_error

logger = get_logger(__name__)

JOB_COLUMNS = (
    "user_id, id, status, run_when, created, started, finished, events, report, stats, error, delivery, evidence, "
    "own_sources"
)
OFF_NOTE = "Dieses Modell verwendet keine eigenen Quellen; die angehängten Dateien und Wissensbasen werden ignoriert."
ONLY_NOTE = (
    "Dieses Modell arbeitet nur mit eigenen Quellen. Bitte laden Sie eine Datei hoch "
    "oder hängen Sie eine Wissensbasis an und stellen Sie die Frage erneut."
)


@dataclass(frozen=True, slots=True)
class WordFile:
    """A rendered report as Word file."""

    data: bytes
    filename: str


def report_title(report: str | None) -> str:
    """Return the first level-1 heading of a report, or "Bericht"."""
    return next((ln[2:].strip() for ln in (report or "").splitlines() if ln.startswith("# ")), "Bericht")


class ResearchService:
    """Drafts, jobs and their views; the engine is reached through `agent_factory`.

    Attributes:
        settings: Service configuration.
        presets: Presets and budgets of the engine.
        db: The jobs and drafts database.
    """

    def __init__(
        self,
        settings: AppConfig,
        presets: Config,
        db: DB,
        agent_factory: Callable[..., Any],
        usage: UsageTrackingService,
    ) -> None:
        """Initialize the service.

        Args:
            settings: Service configuration.
            presets: Presets and budgets of the engine.
            db: The jobs and drafts database.
            agent_factory: ``agent_factory(job_id, on_event=None)`` returns an object with clarify, plan, edit_plan.
            usage: Usage events with pseudonymised user ids.
        """
        self.settings = settings
        self.presets = presets
        self.db = db
        self.agent_factory = agent_factory
        self.usage = usage

    def _own_for(
        self, preset: str, own: OwnSourcesRequest | None, preview: bool
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Apply the preset's own-sources rule (prefer | only | off) and return (stored, info).

        Raises:
            ApiErrorException: 422 if the preset works only with own sources and none are usable.
        """
        mode = self.presets.presets[preset].own_sources
        stored, info = prepare(own, self.settings, preview=preview)
        if mode == "off":
            if own and (own.files or own.collections):
                return None, {"files": [], "collections": [], "mode": mode, "notes": [OFF_NOTE]}
            return None, info
        if mode == "only" and not stored:
            why = " ".join((info or {}).get("notes", []))
            raise user_error(ONLY_NOTE + (" " + why if why else ""))
        return stored, ({**info, "mode": mode} if info else None)

    def _engine_own(self, stored: dict[str, Any] | None) -> dict[str, Any]:
        """Keyword arguments that pass stored own sources to the engine."""
        return {"own": to_engine(stored, self.settings)} if stored else {}

    def list_presets(self) -> list[dict[str, str]]:
        """Return name, description and engine of every preset."""
        return [
            {"name": p.name, "description": p.description, "engine": p.engine} for p in self.presets.presets.values()
        ]

    async def clarify(self, request: ClarifyRequest) -> dict[str, Any]:
        """Return up to two clarifying questions and the assumptions made without an answer.

        With `suggest_preset` the answer also has the preset that fits the task best ("preset", None if none fits).
        """
        agent = self.agent_factory("clarify-" + uuid.uuid4().hex[:8])
        if request.suggest_preset:
            # No preset chosen yet: the model sees the attached sources without any preset's rule.
            stored, _ = prepare(request.own_sources, self.settings, preview=True)
            presets = {p.name: p.description for p in self.presets.presets.values()}
            return await agent.clarify(request.task, presets=presets, **self._engine_own(stored))
        stored = None
        if request.preset in self.presets.presets:
            stored, _ = self._own_for(request.preset, request.own_sources, preview=True)
        return await agent.clarify(request.task, **self._engine_own(stored))

    async def create_plan(self, request: CreatePlanRequest) -> dict[str, Any]:
        """Plan a research and store it as a draft.

        Raises:
            ApiErrorException: 400 for an unknown preset or depth, 422 from the own-sources rule.
        """
        if request.preset not in self.presets.presets or request.depth not in self.presets.budgets:
            raise invalid_request("unknown preset or depth")
        agent = self.agent_factory("plan-" + uuid.uuid4().hex[:8])
        stored, info = self._own_for(request.preset, request.own_sources, preview=True)
        draft = new_id()
        plan = await agent.plan(
            request.task,
            self.presets.presets[request.preset],
            self.presets.budgets[request.depth],
            **self._engine_own(stored),
        )
        await self.db.execute(
            "insert into drafts (id, task, preset, depth, plan, created, own_sources, user_id) "
            "values (:i,:t,:p,:d,:pl,:c,:o,:u)",
            i=draft,
            t=request.task,
            p=request.preset,
            d=request.depth,
            pl=json.dumps(plan, ensure_ascii=False),
            c=time.time(),
            o=json.dumps(stored, ensure_ascii=False) if stored else None,
            u=request.user_id,
        )
        self.usage.log_event("research.plan", request.user_id, preset=request.preset, depth=request.depth)
        return {"draft_id": draft, "plan": plan, "own_sources": info}

    async def _draft(self, draft_id: str, user_id: str | None) -> dict[str, Any]:
        """Return a draft; a draft of another user counts as unknown.

        Raises:
            ApiErrorException: 404 if the draft does not exist or belongs to someone else.
        """
        rows = await self.db.run("select * from drafts where id=:i", i=draft_id)
        if not rows or (rows[0].get("user_id") and user_id and rows[0]["user_id"] != user_id):
            raise not_found("unknown draft")
        return rows[0]

    async def edit_plan(self, draft_id: str, request: EditPlanRequest) -> dict[str, Any]:
        """Change a draft plan as the user asked and store the new plan."""
        draft = await self._draft(draft_id, request.user_id)
        plan = await self.agent_factory("plan-" + draft_id).edit_plan(
            json.loads(draft["plan"]),
            request.instruction,
            draft["task"],
            self.presets.presets[draft["preset"]],
            self.presets.budgets[draft["depth"]],
        )
        await self.db.execute(
            "update drafts set plan=:p where id=:i", p=json.dumps(plan, ensure_ascii=False), i=draft_id
        )
        info = describe(json.loads(draft.get("own_sources") or "null"))
        mode = self.presets.presets[draft["preset"]].own_sources
        return {"draft_id": draft_id, "plan": plan, "own_sources": {**info, "mode": mode} if info else None}

    async def _queued_view(self, job: dict[str, Any], own_info: dict[str, Any] | None) -> dict[str, Any]:
        """The answer to a submit: job id, status, when it runs and how many jobs are ahead."""
        ahead = await self.db.run(
            "select count(*) n from jobs where status in ('queued','starting','running') and created<:t",
            t=job["created"],
        )
        return {
            "job_id": job["id"],
            "status": job["status"],
            "when": job["run_when"],
            "ahead": ahead[0]["n"],
            "window": self.settings.night_window,
            "own_sources": own_info,
        }

    async def _job_of_draft(self, draft_id: str) -> dict[str, Any] | None:
        rows = await self.db.run(
            "select id, status, run_when, created from jobs where draft=:d order by created limit 1", d=draft_id
        )
        return rows[0] if rows else None

    async def submit(self, request: SubmitJobRequest) -> dict[str, Any]:
        """Queue the job of an approved draft; a repeated submit returns the existing job.

        Raises:
            ApiErrorException: 404 for an unknown or foreign draft, 422 from the own-sources rule, 429 over quota.
        """
        draft = await self._draft(request.draft_id, request.user_id)
        prior = await self._job_of_draft(draft["id"])
        if prior:
            return await self._queued_view(prior, None)
        stored, info = self._own_for(draft["preset"], request.own_sources, preview=False)
        when = request.when
        if (
            when == RunWhen.NOW
            and not in_window(self.settings)
            and not runs_by_day(self.presets, draft["preset"], draft["depth"])
        ):
            when = RunWhen.NIGHT
        job_id, now, quota = new_id(), time.time(), self.settings.quota_per_day
        # quota and one-job-per-draft are part of the insert, so that parallel requests cannot slip past a read
        inserted = await self.db.execute(
            "insert into jobs (id, draft, task, preset, depth, plan, user_id, chat_id, message_id, run_when, status,"
            " created, events, own_sources) select :id,:dr,:t,:p,:d,:pl,:u,:c,:m,:w,'queued',"
            "cast(:cr as double precision),'[]',:o where not exists (select 1 from jobs where draft=:dr) "
            "and (cast(:q as integer) = 0 or (select count(*) from jobs where user_id=:u "
            "and created>cast(:since as double precision)) < :q)",
            id=job_id,
            dr=draft["id"],
            t=draft["task"],
            p=draft["preset"],
            d=draft["depth"],
            pl=draft["plan"],
            u=request.user_id,
            c=request.chat_id,
            m=request.message_id,
            w=str(when),
            cr=now,
            o=json.dumps(stored, ensure_ascii=False) if stored else None,
            q=quota,
            since=now - 86400,
        )
        if not inserted:
            prior = await self._job_of_draft(draft["id"])
            if prior:
                return await self._queued_view(prior, None)
            raise rate_limited(f"quota of {quota} research jobs per day reached")
        files, collections = (stored or {}).get("files", []), (stored or {}).get("collections", [])
        logger.info(
            "job queued",
            job=job_id,
            preset=draft["preset"],
            depth=draft["depth"],
            when=str(when),
            own_files=len(files),
            own_collections=len(collections),
        )
        self.usage.log_event("research.submit", request.user_id, preset=draft["preset"], depth=draft["depth"])
        return await self._queued_view({"id": job_id, "status": "queued", "run_when": str(when), "created": now}, info)

    async def lookup(self, chat_id: str, message_id: str, user_id: str | None) -> dict[str, str]:
        """Return the newest job that delivers into a chat message (used by the Word download action).

        Raises:
            ApiErrorException: 404 if there is none, or it belongs to someone else.
        """
        rows = await self.db.run(
            "select id, status, user_id from jobs where chat_id=:c and message_id=:m order by created desc limit 1",
            c=chat_id,
            m=message_id,
        )
        if not rows or (user_id and rows[0]["user_id"] != user_id):
            raise not_found("no job for this message")
        return {"job_id": rows[0]["id"], "status": rows[0]["status"]}

    async def _job(self, job_id: str, user_id: str | None, columns: str) -> dict[str, Any]:
        """Return the job's row; a job of another user counts as unknown.

        Raises:
            ApiErrorException: 404 if the job does not exist or belongs to someone else.
        """
        rows = await self.db.run(f"select {columns} from jobs where id=:i", i=job_id)
        if not rows or (user_id and rows[0]["user_id"] != user_id):
            raise not_found("unknown job")
        return rows[0]

    async def job_view(self, job_id: str, user_id: str | None) -> dict[str, Any]:
        """Return status, progress and, when done, the report with its sources for the chat."""
        job = await self._job(job_id, user_id, JOB_COLUMNS)
        job.pop("user_id", None)
        own = describe(json.loads(job.pop("own_sources") or "null"))
        evidence = json.loads(job.pop("evidence") or "null")
        done = job["status"] == "done"
        sources = build_sources(job["report"], evidence) if job["report"] and done else []
        return {
            **job,
            "events": json.loads(job["events"] or "[]")[-10:],
            "stats": json.loads(job["stats"] or "null"),
            "own_sources": own,
            "sources": sources,
            "ui_report": ui_markdown(job["report"], sources) if job["report"] else job["report"],
            "owui_events": owui_source_events(sources),
            "docx": f"/jobs/{job_id}/report.docx" if done else None,
            "docx_name": docx_filename(report_title(job["report"]), job["finished"], self.settings.timezone)
            if done
            else None,
        }

    async def word_file(self, job_id: str, user_id: str | None) -> WordFile:
        """Render the finished report as Word file.

        Raises:
            ApiErrorException: 404 for an unknown or foreign job, 409 while there is no report.
        """
        job = await self._job(job_id, user_id, "user_id, status, report, evidence, task, preset, depth, finished")
        if job["status"] != "done" or not job["report"]:
            raise conflict(f"job is {job['status']}, no report yet")
        sources = build_sources(job["report"], json.loads(job["evidence"] or "null"))
        data = render_docx(
            job["report"],
            sources,
            task=job["task"],
            preset=job["preset"],
            depth=job["depth"],
            finished=job["finished"],
            tz=self.settings.timezone,
        )
        return WordFile(data, docx_filename(report_title(job["report"]), job["finished"], self.settings.timezone))

    async def evidence(self, job_id: str, user_id: str | None) -> dict[str, Any]:
        """Return the evidence ledger of a job (cards with quotes and URLs)."""
        job = await self._job(job_id, user_id, "user_id, evidence")
        return json.loads(job["evidence"] or "{}")

    async def metrics(self) -> str:
        """Return job counts and the night-window flag in the Prometheus text format."""
        rows = await self.db.run("select status, count(*) n from jobs group by status")
        counts = {r["status"]: r["n"] for r in rows}
        lines = ["# TYPE research_jobs gauge"]
        lines += [
            f'research_jobs{{status="{s}"}} {counts.get(s, 0)}'
            for s in ("queued", "starting", "running", "done", "error")
        ]
        lines += ["# TYPE research_in_night_window gauge", f"research_in_night_window {int(in_window(self.settings))}"]
        return "\n".join(lines) + "\n"
