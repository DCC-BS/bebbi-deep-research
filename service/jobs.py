"""Job queue: night window, daytime rule, workers, delivery into the Open WebUI chat."""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from research_agent import LLM, ResearchAgent, Tools

from .db import DB
from .logging_config import log
from .own import to_engine
from .report import build_sources, owui_source_events, ui_markdown
from .settings import Settings

logger = logging.getLogger("service.jobs")
LIVE = ("starting", "running")


def describe_error(e: BaseException) -> str:
    """German, user-facing reason for a failure (shown in the chat); the technical detail follows in short form."""
    import openai

    detail = " ".join(str(getattr(e, "message", None) or e).split())[:200]
    if isinstance(e, (openai.APIConnectionError, openai.APITimeoutError)):
        reason = "Das Sprachmodell ist nicht erreichbar."
    elif isinstance(e, (openai.NotFoundError, openai.BadRequestError, openai.AuthenticationError, openai.PermissionDeniedError)):
        reason = "Das Sprachmodell lehnt die Anfrage ab (Modellname oder Zugang falsch konfiguriert)."
    elif isinstance(e, httpx.HTTPError):
        reason = "Ein Dienst der Recherche (Suche oder Seitenabruf) ist nicht erreichbar."
    else:
        reason = "Unerwarteter Fehler."
    return f"{reason} Bitte versuchen Sie es später erneut oder wenden Sie sich an den Betrieb. (Technisch: {type(e).__name__}: {detail})"


def in_window(settings: Settings, now: datetime | None = None) -> bool:
    start, end = settings.window()
    t = (now or datetime.now(ZoneInfo(settings.timezone))).time()
    return (t >= start or t < end) if start > end else start <= t < end


def runs_by_day(cfg, preset: str, depth: str) -> bool:
    """Office hours: only quick runs of the focus engine (about 20-50k tokens, 2-3 min) start right away."""
    p = cfg.presets.get(preset)
    return depth == "quick" and p is not None and p.engine == "focus"


def make_agent_factory(cfg, settings: Settings) -> Callable[..., ResearchAgent]:
    def make_agent(job_id: str, on_event=None) -> ResearchAgent:
        llm = LLM(base_url=settings.llm_base_url, api_key=settings.llm_api_key.replace("{job}", job_id),
                  models={"think": settings.model_think, "fast": settings.model_fast, "embed": settings.model_embed},
                  extra_body={r: b for r, b in (("think", settings.extra_body_think), ("fast", settings.extra_body_fast)) if b})
        tools = Tools(search_url=settings.search_url, search_key=settings.search_key.replace("{job}", job_id),
                      firecrawl_url=settings.firecrawl_url, firecrawl_key=settings.firecrawl_key)
        return ResearchAgent(cfg, llm, tools, on_event=on_event)
    return make_agent


async def close_agent(agent) -> None:
    """Release HTTP connections of one agent (MCP sessions, search/fetch client)."""
    for c in list(getattr(agent, "_mcp_clients", {}).values()):
        try:
            c.http.close()
        except Exception:
            pass
    http = getattr(getattr(agent, "tools", None), "http", None)
    if http is not None:
        try:
            await http.aclose()
        except Exception:
            pass


class JobRunner:
    def __init__(self, settings: Settings, db: DB, cfg, agent_factory: Callable[..., ResearchAgent], http_factory=None):
        self.settings, self.db, self.cfg, self.make_agent = settings, db, cfg, agent_factory
        self.http_factory = http_factory or (lambda: httpx.AsyncClient(timeout=30))
        self.tasks: dict[str, asyncio.Task] = {}
        self._loop_task: asyncio.Task | None = None
        self._stopping = False
        self.retry_delay = 2.0

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Graceful shutdown: stop claiming, cancel running jobs and put them back into the queue."""
        self._stopping = True
        if self._loop_task:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
        for t in list(self.tasks.values()):
            t.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    # ---------------------------------------------------------------- loop
    async def _loop(self) -> None:
        last_purge = 0.0
        while True:
            try:
                await self.tick()
                if time.time() - last_purge > 3600:
                    last_purge = time.time()
                    await self.db.execute("delete from drafts where created < :t", t=time.time() - 14 * 86400)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(logger, "worker loop error", logging.ERROR, error=repr(e))
            await asyncio.sleep(self.settings.poll_seconds)

    async def tick(self) -> None:
        s = self.settings
        now = time.time()
        # heartbeat for our jobs; jobs of dead replicas (no heartbeat) go back to the queue
        for jid in list(self.tasks):
            await self.db.execute("update jobs set heartbeat=:h where id=:i", h=now, i=jid)
        n = await self.db.execute("update jobs set status='queued' where status in ('starting','running') "
                                  "and coalesce(heartbeat, created) < :t", t=now - s.stale_seconds)
        if n:
            log(logger, "re-queued stale jobs", logging.WARNING, count=n)
        self.tasks = {k: t for k, t in self.tasks.items() if not t.done()}
        window_ok = in_window(s)
        free = (s.workers if window_ok else s.day_workers) - len(self.tasks)
        if free <= 0:
            return
        rows = await self.db.run("select id from jobs where status='queued' and (run_when='now' or :w = 1) "
                                 "order by created limit :n", w=1 if window_ok else 0, n=free)
        for r in rows:
            # atomic claim: with several replicas only one wins
            if await self.db.execute("update jobs set status='starting', heartbeat=:h where id=:i and status='queued'",
                                     h=now, i=r["id"]):
                self.tasks[r["id"]] = asyncio.create_task(self.run_job(r["id"]))

    # ----------------------------------------------------------------- job
    async def run_job(self, job_id: str) -> None:
        job = (await self.db.run("select * from jobs where id=:i", i=job_id))[0]
        events: list[dict] = []

        async def on_event(kind: str, text: str) -> None:
            events.append({"t": round(time.time(), 1), "kind": kind, "text": text})
            await self.db.execute("update jobs set events=:e, heartbeat=:h where id=:i",
                                  e=json.dumps(events[-50:], ensure_ascii=False), h=time.time(), i=job_id)

        started = time.time()
        await self.db.execute("update jobs set status='running', started=:t where id=:i", t=started, i=job_id)
        log(logger, "job started", job=job_id, preset=job["preset"], depth=job["depth"], user=job["user_id"])
        agent = None
        try:
            agent = self.make_agent(job_id, on_event)
            own = to_engine(json.loads(job.get("own_sources") or "null"), self.settings, self.http_factory)
            res = await agent.run(job["task"], job["preset"], job["depth"], plan=json.loads(job["plan"]),
                                  **({"own": own} if own else {}))
            stats = {**res.stats, "usage": res.usage}
            await self.db.execute("update jobs set status='done', finished=:t, report=:r, stats=:s, evidence=:e where id=:i",
                                  t=time.time(), r=res.report, s=json.dumps(stats, ensure_ascii=False),
                                  e=json.dumps(res.ledger, ensure_ascii=False), i=job_id)
            u = res.usage or {}
            log(logger, "job done", job=job_id, runtime_s=round(time.time() - started, 1), calls=u.get("calls"),
                prompt_tokens=u.get("prompt_tokens"), completion_tokens=u.get("completion_tokens"))
            sources = build_sources(res.report, res.ledger)
            await self.deliver(job, ui_markdown(res.report, sources), sources)
        except asyncio.CancelledError:
            # shutdown: the job starts again from the beginning on the next start
            await asyncio.shield(self.db.execute("update jobs set status='queued' where id=:i", i=job_id))
            log(logger, "job re-queued on shutdown", logging.WARNING, job=job_id)
            raise
        except Exception as e:
            log(logger, "job failed", logging.ERROR, job=job_id, error=repr(e)[:500])
            message = describe_error(e)
            await self.db.execute("update jobs set status='error', finished=:t, error=:e where id=:i",
                                  t=time.time(), e=message, i=job_id)
            await self.deliver(job, f"Die Recherche ist fehlgeschlagen. {message}")
        finally:
            if agent is not None:
                await asyncio.shield(close_agent(agent))

    async def _has_sources(self, chat_id: str, message_id: str) -> bool:
        """True if the Open WebUI message already carries sources (the pipe returned the report itself and emitted them)."""
        try:
            async with self.http_factory() as http:
                r = await http.get(f"{self.settings.owui_url}/api/v1/chats/{chat_id}", headers={"Authorization": f"Bearer {self.settings.owui_api_key}"})
            return r.status_code == 200 and bool(r.json()["chat"]["history"]["messages"][message_id].get("sources"))
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return False

    async def deliver(self, job: dict, content: str, sources: list[dict] | None = None) -> None:
        """Write the finished report into the Open WebUI assistant message (only if configured and the job came from a chat).

        Two kinds of calls, both with the admin key: one `source` event per cited source (Open WebUI stores them in the
        `sources` of the message and shows the citation badges and the source list), then the message text. Sources go
        first so that the badges exist when the text appears. Retried 3 times, each step only once it succeeded; the
        outcome is stored in jobs.delivery. The pipe returns the report on the next message anyway."""
        s = self.settings
        if not (s.owui_url and s.owui_api_key and job.get("chat_id") and job.get("message_id")):
            await self.db.execute("update jobs set delivery='skipped' where id=:i", i=job["id"])
            log(logger, "delivery skipped", job=job["id"], reason="no OWUI_URL/OWUI_API_KEY or job without chat")
            return
        base = f"{s.owui_url}/api/v1/chats/{job['chat_id']}/messages/{job['message_id']}"
        events = owui_source_events(sources or [])
        if events and await self._has_sources(job["chat_id"], job["message_id"]):    # the pipe was faster: stored twice they would double the list
            events = []
        steps = [(f"{base}/event", ev) for ev in events] + [(base, {"content": content})]
        outcome = "failed"
        for attempt in range(3):
            try:
                async with self.http_factory() as http:
                    while steps:
                        url, payload = steps[0]
                        r = await http.post(url, json=payload, headers={"Authorization": f"Bearer {s.owui_api_key}"})
                        if r.status_code >= 300 or (url.endswith("/event") and r.text.strip() != "true"):
                            outcome = f"failed: HTTP {r.status_code} {r.text[:200]}"
                            break
                        steps.pop(0)
                if not steps:
                    outcome = "ok"
                    break
            except httpx.HTTPError as e:
                outcome = f"failed: {type(e).__name__}: {e}"[:300]
            if attempt < 2:
                await asyncio.sleep(self.retry_delay * (attempt + 1))
        level = logging.INFO if outcome == "ok" else logging.ERROR
        log(logger, "delivery " + outcome[:60], level, job=job["id"], chat=job["chat_id"], sources=len(sources or []))
        await self.db.execute("update jobs set delivery=:d where id=:i", d=outcome[:300], i=job["id"])


def new_id() -> str:
    return uuid.uuid4().hex[:12]
