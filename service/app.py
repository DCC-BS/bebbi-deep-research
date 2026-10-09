"""research-agent job service: chat-turn helpers (clarify, plan, edit) and a job queue with a night window.

Used by the Open WebUI pipe (pipe/deep_research.py) and by other apps (n8n). Configuration: environment
variables, see service/settings.py and docs/operations.md.

  uv run bebbi-deep-research            # or: uvicorn service.app:create_app --factory
"""

import hmac
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from research_agent import load_config

from .db import DB
from .docx_export import MIME, docx_filename, render_docx
from .jobs import JobRunner, describe_error, in_window, make_agent_factory, new_id, runs_by_day
from .logging_config import log, setup_logging
from .report import build_sources, owui_source_events, ui_markdown
from .settings import Settings

logger = logging.getLogger("service.app")


class TaskIn(BaseModel):
    task: str
    preset: str = "verwaltung_web"
    depth: str = "standard"


class EditIn(BaseModel):
    instruction: str


class JobIn(BaseModel):
    draft_id: str
    user_id: str
    chat_id: str | None = None
    message_id: str | None = None
    when: str = "night"           # now | night


def _title(report: str) -> str:
    return next((ln[2:].strip() for ln in (report or "").splitlines() if ln.startswith("# ")), "Bericht")


def create_app(settings: Settings | None = None, agent_factory=None, db: DB | None = None, http_factory=None) -> FastAPI:
    """agent_factory(job_id, on_event) -> object with clarify/plan/edit_plan/run (default: the research agent);
    injectable for tests."""
    settings = settings or Settings.from_env()
    problems = settings.problems()
    cfg = None
    if not any("RA_PRESETS" in p for p in problems):
        try:
            cfg = load_config(settings.presets_path)
        except Exception as e:      # reported by /readyz instead of crashing the pod in a loop
            problems.append(f"presets cannot be loaded: {e}")
    db = db or DB(settings.database_url)
    factory = agent_factory or (make_agent_factory(cfg, settings) if cfg else None)
    runner = JobRunner(settings, db, cfg, factory, http_factory) if cfg and factory else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(settings.log_level)
        log(logger, "starting", window=settings.night_window, tz=settings.timezone, workers=settings.workers,
            day_workers=settings.day_workers, quota=settings.quota_per_day, db=db.url.split("://")[0],
            problems=problems)
        await db.init()
        if runner:
            await runner.start()
        yield
        log(logger, "shutting down: re-queueing running jobs")
        if runner:
            await runner.stop()
        await db.close()

    app = FastAPI(title="bebbi-deep-research", lifespan=lifespan)
    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        """LLM or tool failures in the chat-turn endpoints: a readable German detail for the pipe instead of a bare 500."""
        logger.error("unhandled error in %s", request.url.path, exc_info=exc)
        return JSONResponse({"detail": "Die Anfrage konnte nicht verarbeitet werden. " + describe_error(exc)}, status_code=502)

    app.state.settings, app.state.db, app.state.runner, app.state.cfg = settings, db, runner, cfg

    def auth(request: Request) -> None:
        if not settings.token or not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {settings.token}"):
            raise HTTPException(401, "invalid token")

    def agent(job_id: str):
        if runner is None:
            raise HTTPException(503, "service is not configured: " + "; ".join(problems))
        return factory(job_id)

    # ------------------------------------------------------------ probes
    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response):
        errs = list(problems)
        try:
            await db.ping()
        except Exception as e:
            errs.append(f"database: {type(e).__name__}")
        if errs:
            response.status_code = 503
        return {"status": "error" if errs else "ok", "problems": errs}

    @app.get("/metrics")
    async def metrics():
        rows = await db.run("select status, count(*) n from jobs group by status")
        counts = {r["status"]: r["n"] for r in rows}
        lines = ["# TYPE research_jobs gauge"]
        lines += [f'research_jobs{{status="{s}"}} {counts.get(s, 0)}' for s in ("queued", "starting", "running", "done", "error")]
        lines += ["# TYPE research_in_night_window gauge", f"research_in_night_window {int(in_window(settings))}"]
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    # --------------------------------------------------------------- API
    @app.get("/presets", dependencies=[Depends(auth)])
    async def presets():
        return [{"name": p.name, "description": p.description, "engine": p.engine} for p in (cfg.presets.values() if cfg else [])]

    @app.post("/clarify", dependencies=[Depends(auth)])
    async def clarify(body: TaskIn):
        return await agent("clarify-" + uuid.uuid4().hex[:8]).clarify(body.task)

    @app.post("/plans", dependencies=[Depends(auth)])
    async def create_plan(body: TaskIn):
        a = agent("plan-" + uuid.uuid4().hex[:8])
        if body.preset not in cfg.presets or body.depth not in cfg.budgets:
            raise HTTPException(400, "unknown preset or depth")
        draft = new_id()
        plan = await a.plan(body.task, cfg.presets[body.preset], cfg.budgets[body.depth])
        await db.execute("insert into drafts (id, task, preset, depth, plan, created) values (:i,:t,:p,:d,:pl,:c)", i=draft,
                         t=body.task, p=body.preset, d=body.depth, pl=json.dumps(plan, ensure_ascii=False), c=time.time())
        return {"draft_id": draft, "plan": plan}

    @app.post("/plans/{draft_id}/edit", dependencies=[Depends(auth)])
    async def edit_plan(draft_id: str, body: EditIn):
        rows = await db.run("select * from drafts where id=:i", i=draft_id)
        if not rows:
            raise HTTPException(404, "unknown draft")
        d = rows[0]
        plan = await agent("plan-" + draft_id).edit_plan(json.loads(d["plan"]), body.instruction, d["task"],
                                                         cfg.presets[d["preset"]], cfg.budgets[d["depth"]])
        await db.execute("update drafts set plan=:p where id=:i", p=json.dumps(plan, ensure_ascii=False), i=draft_id)
        return {"draft_id": draft_id, "plan": plan}

    @app.post("/jobs", dependencies=[Depends(auth)])
    async def submit(body: JobIn):
        rows = await db.run("select * from drafts where id=:i", i=body.draft_id)
        if not rows:
            raise HTTPException(404, "unknown draft")
        quota = settings.quota_per_day
        if quota:
            used = (await db.run("select count(*) n from jobs where user_id=:u and created>:t", u=body.user_id,
                                 t=time.time() - 86400))[0]["n"]
            if used >= quota:
                raise HTTPException(429, f"quota of {quota} research jobs per day reached")
        d, job_id = rows[0], new_id()
        when = body.when if body.when in ("now", "night") else "night"
        if when == "now" and not in_window(settings) and not runs_by_day(cfg, d["preset"], d["depth"]):
            when = "night"
        now = time.time()
        await db.execute("insert into jobs (id, draft, task, preset, depth, plan, user_id, chat_id, message_id, run_when, status,"
                         " created, events) values (:id,:dr,:t,:p,:d,:pl,:u,:c,:m,:w,'queued',:cr,'[]')", id=job_id, dr=d["id"],
                         t=d["task"], p=d["preset"], d=d["depth"], pl=d["plan"], u=body.user_id, c=body.chat_id,
                         m=body.message_id, w=when, cr=now)
        ahead = (await db.run("select count(*) n from jobs where status in ('queued','starting','running') and created<:t", t=now))[0]["n"]
        log(logger, "job queued", job=job_id, preset=d["preset"], depth=d["depth"], when=when, user=body.user_id)
        return {"job_id": job_id, "status": "queued", "when": when, "ahead": ahead, "window": settings.night_window}

    @app.get("/jobs/lookup", dependencies=[Depends(auth)])
    async def job_lookup(chat_id: str, message_id: str, user_id: str | None = None):
        """The newest job that delivers into this chat message (used by the Open WebUI download action)."""
        rows = await db.run("select id, status, user_id from jobs where chat_id=:c and message_id=:m order by created desc limit 1",
                            c=chat_id, m=message_id)
        if not rows or (user_id and rows[0]["user_id"] != user_id):
            raise HTTPException(404, "no job for this message")
        return {"job_id": rows[0]["id"], "status": rows[0]["status"]}

    @app.get("/jobs/{job_id}", dependencies=[Depends(auth)])
    async def job_status(job_id: str):
        rows = await db.run("select id, status, run_when, created, started, finished, events, report, stats, error, delivery,"
                            " evidence from jobs where id=:i", i=job_id)
        if not rows:
            raise HTTPException(404, "unknown job")
        j = rows[0]
        evidence = json.loads(j.pop("evidence") or "null")
        sources = build_sources(j["report"], evidence) if j["report"] and j["status"] == "done" else []
        return {**j, "events": json.loads(j["events"] or "[]")[-10:], "stats": json.loads(j["stats"] or "null"),
                "sources": sources, "ui_report": ui_markdown(j["report"], sources) if sources else j["report"],
                "owui_events": owui_source_events(sources), "docx": f"/jobs/{job_id}/report.docx" if j["status"] == "done" else None,
                "docx_name": docx_filename(_title(j["report"]), j["finished"], settings.timezone) if j["status"] == "done" else None}

    @app.get("/jobs/{job_id}/report.docx", dependencies=[Depends(auth)])
    async def job_docx(job_id: str):
        rows = await db.run("select status, report, evidence, task, preset, depth, finished from jobs where id=:i", i=job_id)
        if not rows:
            raise HTTPException(404, "unknown job")
        j = rows[0]
        if j["status"] != "done" or not j["report"]:
            raise HTTPException(409, f"job is {j['status']}, no report yet")
        sources = build_sources(j["report"], json.loads(j["evidence"] or "null"))
        data = render_docx(j["report"], sources, task=j["task"], preset=j["preset"], depth=j["depth"], finished=j["finished"],
                           tz=settings.timezone)
        name = docx_filename(_title(j["report"]), j["finished"], settings.timezone)
        return Response(data, media_type=MIME, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/jobs/{job_id}/evidence", dependencies=[Depends(auth)])
    async def job_evidence(job_id: str):
        rows = await db.run("select evidence from jobs where id=:i", i=job_id)
        if not rows:
            raise HTTPException(404, "unknown job")
        return json.loads(rows[0]["evidence"] or "{}")

    return app
