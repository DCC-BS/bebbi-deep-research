"""Job queue: night window, daytime rule, workers, delivery into the Open WebUI chat."""

import asyncio
import json
import re
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import openai
from dcc_backend_common.logger import get_logger
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UnexpectedModelBehavior

from bebbi_deep_research.engine import LLM, ResearchAgent, Tools
from bebbi_deep_research.engine.llm import LlmSettings
from bebbi_deep_research.services.db import DB
from bebbi_deep_research.services.own_sources import to_engine
from bebbi_deep_research.services.report import build_sources, owui_source_events, ui_markdown
from bebbi_deep_research.utils.app_config import AppConfig

logger = get_logger(__name__)
LIVE = ("starting", "running")
MAX_ATTEMPTS = 3  # a job that was interrupted (pod killed, stalled) this often is given up with an error
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
    """Office hours: only quick runs of the focus engine (about 20-50k tokens, 2-3 min) start right away."""
    p = cfg.presets.get(preset)
    return depth == "quick" and p is not None and p.engine == "focus"


def make_agent_factory(cfg, settings: AppConfig) -> Callable[..., ResearchAgent]:
    """Return ``make_agent(job_id, on_event=None)``, which builds a research agent with its own LLM and tools.

    "{job}" in the LLM and search keys is replaced by the job id (gateway metering).
    """

    def make_agent(job_id: str, on_event=None) -> ResearchAgent:
        llm = LLM(
            LlmSettings(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key.replace("{job}", job_id),
                embed_base_url=settings.llm_embed_base_url,
                embed_api_key=settings.llm_embed_api_key.replace("{job}", job_id),
                models={"think": settings.model_think, "fast": settings.model_fast, "embed": settings.model_embed},
                extra_body={
                    r: b for r, b in (("think", settings.extra_body_think), ("fast", settings.extra_body_fast)) if b
                },
            )
        )
        tools = Tools(
            search_url=settings.search_url,
            search_key=settings.search_key.replace("{job}", job_id),
            firecrawl_url=settings.firecrawl_url,
            firecrawl_key=settings.firecrawl_key,
        )
        return ResearchAgent(cfg, llm, tools, on_event=on_event)

    return make_agent


async def close_agent(agent) -> None:
    """Release HTTP connections of one agent (MCP sessions, search/fetch client)."""
    for c in list(getattr(agent, "_mcp_clients", {}).values()):
        try:
            c.http.close()
        except Exception:
            pass
    llm = getattr(agent, "llm", None)
    if llm is not None and hasattr(llm, "aclose"):
        try:
            await llm.aclose()
        except Exception as e:  # closing is best effort; the job result is already stored
            logger.warning("llm clients not closed", error=repr(e)[:200])
    http = getattr(getattr(agent, "tools", None), "http", None)
    if http is not None:
        try:
            await http.aclose()
        except Exception:
            pass


class JobRunner:
    """Worker loop: claims queued jobs (night window, daytime rule), runs them, re-queues stale ones, delivers."""

    def __init__(
        self, settings: AppConfig, db: DB, cfg, agent_factory: Callable[..., ResearchAgent], http_factory=None
    ):
        """Create the runner (the loop starts with `start`).

        Args:
            settings: Service configuration.
            db: Jobs database.
            cfg: Presets and budgets.
            agent_factory: ``agent_factory(job_id, on_event)`` builds the research agent of a job.
            http_factory: Returns the HTTP client for Open WebUI calls (tests pass a fake).
        """
        self.settings, self.db, self.cfg, self.make_agent = settings, db, cfg, agent_factory
        self.http_factory = http_factory or (lambda: httpx.AsyncClient(timeout=30))
        self.tasks: dict[str, asyncio.Task] = {}
        # job id -> claim token of this run (only the claiming run may write the final status)
        self.owners: dict[str, str] = {}
        self.day: set[str] = set()  # running jobs that took a daytime slot
        self._bg: set[asyncio.Task] = set()  # re-deliveries
        self._loop_task: asyncio.Task | None = None
        self._stopping = False
        self._last_sweep = 0.0
        self.retry_delay = 2.0
        self.sweep_after = 300.0  # a finished job whose delivery failed or is missing is delivered again after this

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        """Start the worker loop."""
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the loop and cancel running jobs (they are re-queued for the next start)."""
        """Graceful shutdown: stop claiming, cancel running jobs and put them back into the queue."""
        self._stopping = True
        if self._loop_task:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
        pending = list(self.tasks.values()) + list(self._bg)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

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
                logger.error("worker loop error", error=repr(e))
            await asyncio.sleep(self.settings.poll_seconds)

    async def tick(self) -> None:
        """Run one loop step: heartbeats, stale jobs, claiming new jobs, the delivery sweep."""
        s = self.settings
        now = time.time()
        # heartbeat for our jobs; jobs of dead replicas (no heartbeat) go back to the queue
        for jid in list(self.tasks):
            await self.db.execute(
                "update jobs set heartbeat=:h where id=:i and owner=:o and status in ('starting','running')",
                h=now,
                i=jid,
                o=self.owners.get(jid, ""),
            )
        stale = "status in ('starting','running') and coalesce(heartbeat, created) < :t"
        for r in await self.db.run(
            f"select id, user_id, chat_id, message_id from jobs where {stale} and coalesce(attempts,0) >= :m",
            t=now - s.stale_seconds,
            m=MAX_ATTEMPTS,
        ):
            msg = (
                f"Die Recherche wurde {MAX_ATTEMPTS} Mal unterbrochen und abgebrochen. "
                "Bitte versuchen Sie es erneut oder wenden Sie sich an den Betrieb."
            )
            if await self.db.execute(
                "update jobs set status='error', finished=:f, error=:e, owner=null where id=:i and " + stale,
                f=now,
                e=msg,
                i=r["id"],
                t=now - s.stale_seconds,
            ):
                logger.error("job given up after repeated interruptions", job=r["id"], attempts=MAX_ATTEMPTS)
                await self._safe_deliver(r, f"Die Recherche ist fehlgeschlagen. {msg}")
        n = await self.db.execute(f"update jobs set status='queued', owner=null where {stale}", t=now - s.stale_seconds)
        if n:
            logger.warning("re-queued stale jobs", count=n)
        for k in [k for k, t in self.tasks.items() if t.done()]:
            self.tasks.pop(k), self.owners.pop(k, None), self.day.discard(k)
        window_ok = in_window(s)
        # in the window all workers count; outside only daytime jobs (a night job still running takes no daytime slot)
        free = s.workers - len(self.tasks) if window_ok else s.day_workers - len(self.day)
        if free > 0 and not self._stopping:
            rows = await self.db.run(
                "select id, preset, depth, run_when from jobs where status='queued' "
                "and (run_when='now' or :w = 1) order by created limit 200",
                w=1 if window_ok else 0,
            )
            for r in rows:
                if free <= 0:
                    break
                by_day = runs_by_day(self.cfg, r["preset"], r["depth"])
                if (
                    not window_ok and not by_day
                ):  # the daytime rule is checked when the job starts, not only when it was submitted
                    continue
                # atomic claim: with several replicas only one wins
                token = uuid.uuid4().hex
                if await self.db.execute(
                    "update jobs set status='starting', heartbeat=:h, owner=:o, attempts=coalesce(attempts,0)+1 "
                    "where id=:i and status='queued'",
                    h=now,
                    o=token,
                    i=r["id"],
                ):
                    old = self.tasks.get(r["id"])
                    if (
                        old and not old.done()
                    ):  # a run of this job that another replica gave up on: it must not write any more
                        old.cancel()
                    self.tasks[r["id"]], self.owners[r["id"]] = asyncio.create_task(self.run_job(r["id"], token)), token
                    if by_day:
                        self.day.add(r["id"])
                    free -= 1
        if now - self._last_sweep > 60:
            self._last_sweep = now
            await self._sweep_deliveries(now)

    async def _sweep_deliveries(self, now: float) -> None:
        """Deliver finished jobs again whose delivery failed or was cut off by a shutdown (up to 3 times)."""
        rows = await self.db.run(
            "select * from jobs where status='done' and finished < :a and finished > :b "
            "and coalesce(delivery_tries,0) < 3 and (delivery is null or delivery like 'failed%') "
            "order by finished limit 1",
            a=now - self.sweep_after,
            b=now - 86400,
        )
        for job in rows:
            if await self.db.execute(
                "update jobs set delivery_tries=coalesce(delivery_tries,0)+1 "
                "where id=:i and coalesce(delivery_tries,0)=:k",
                i=job["id"],
                k=job.get("delivery_tries") or 0,
            ):
                logger.info("delivering again", job=job["id"], previous=(job.get("delivery") or "none")[:60])
                t = asyncio.create_task(
                    self._deliver_done(job, job["report"] or "", json.loads(job["evidence"] or "null"))
                )
                self._bg.add(t)
                t.add_done_callback(self._bg.discard)

    # ----------------------------------------------------------------- job
    async def run_job(self, job_id: str, owner: str = "") -> None:
        """Run one claimed job and deliver its report; `owner` is the claim token of this run."""
        guard = (
            "id=:i and owner=:o and status in ('starting','running')"  # only the run that claimed the job may change it
        )
        job = res = None
        events: list[dict] = []

        async def on_event(kind: str, text: str) -> None:
            events.append({"t": round(time.time(), 1), "kind": kind, "text": text})
            try:  # progress is nice to have: a database hiccup must not abort hours of research
                await self.db.execute(
                    "update jobs set events=:e, heartbeat=:h where id=:i and owner=:o",
                    e=json.dumps(events[-50:], ensure_ascii=False),
                    h=time.time(),
                    i=job_id,
                    o=owner,
                )
            except Exception as e:
                logger.warning("progress not stored", job=job_id, error=repr(e)[:200])

        started = time.time()
        agent = None
        try:
            job = (await self.db.run("select * from jobs where id=:i", i=job_id))[0]
            if not await self.db.execute(
                "update jobs set status='running', started=:t where " + guard, t=started, i=job_id, o=owner
            ):
                logger.warning("job no longer ours", job=job_id)
                return
            logger.info("job started", job=job_id, preset=job["preset"], depth=job["depth"])
            agent = self.make_agent(job_id, on_event)
            own = to_engine(json.loads(job.get("own_sources") or "null"), self.settings, self.http_factory)
            async with asyncio.timeout(self.settings.job_timeout_seconds):
                res = await agent.run(
                    job["task"],
                    job["preset"],
                    job["depth"],
                    plan=json.loads(job["plan"]),
                    **({"own": own} if own else {}),
                )
            stats = {**res.stats, "usage": res.usage}
            if not await self.db.execute(
                "update jobs set status='done', finished=:t, report=:r, stats=:s, evidence=:e where " + guard,
                t=time.time(),
                r=res.report,
                s=json.dumps(stats, ensure_ascii=False),
                e=json.dumps(res.ledger, ensure_ascii=False),
                i=job_id,
                o=owner,
            ):
                logger.warning("job no longer ours, result dropped", job=job_id)
                return
            u = res.usage or {}
            logger.info(
                "job done",
                job=job_id,
                runtime_s=round(time.time() - started, 1),
                calls=u.get("calls"),
                prompt_tokens=u.get("prompt_tokens"),
                completion_tokens=u.get("completion_tokens"),
            )
        except asyncio.CancelledError:
            # shutdown: the job starts again from the beginning on the next start (not counted as an attempt)
            await asyncio.shield(
                self.db.execute(
                    "update jobs set status='queued', owner=null, attempts=coalesce(attempts,1)-1 where " + guard,
                    i=job_id,
                    o=owner,
                )
            )
            logger.warning("job re-queued on shutdown", job=job_id)
            raise
        except Exception as e:
            logger.error("job failed", job=job_id, error=repr(e)[:500])
            message = describe_error(e)
            try:
                mine = await self.db.execute(
                    "update jobs set status='error', finished=:t, error=:e where " + guard,
                    t=time.time(),
                    e=message,
                    i=job_id,
                    o=owner,
                )
            except Exception as e2:  # database down as well: the stale-job requeue takes over
                logger.error("job error not stored", job=job_id, error=repr(e2)[:200])
                mine = 0
            if mine and job:
                await self._safe_deliver(job, f"Die Recherche ist fehlgeschlagen. {message}")
            return
        finally:
            if agent is not None:
                await asyncio.shield(close_agent(agent))
        # delivery is separate from the run: a failure or a cancel here never touches the status of a finished job
        await self._deliver_done(job, res.report, res.ledger)

    async def _deliver_done(self, job: dict, report: str, ledger: dict | None) -> None:
        try:
            sources = build_sources(report, ledger)
            await self.deliver(job, ui_markdown(report, sources), sources)
        except Exception as e:
            logger.error("delivery error", job=job["id"], error=repr(e)[:300])
            try:
                await self.db.execute(
                    "update jobs set delivery=:d where id=:i", d=f"failed: {type(e).__name__}: {e}"[:300], i=job["id"]
                )
            except Exception:
                pass

    async def _safe_deliver(self, job: dict, content: str) -> None:
        try:
            await self.deliver(job, content)
        except Exception as e:
            logger.error("delivery error", job=job["id"], error=repr(e)[:300])

    async def deliver(self, job: dict, content: str, sources: list[dict] | None = None) -> None:
        """Write the finished report into the Open WebUI assistant message of the job.

        Only if the delivery endpoint is configured and the job came from a chat. One call to the endpoint that the
        Open WebUI image adds (`POST /api/v1/dcc/deep-research/deliver`, shared secret, not an API key): it stores one
        `source` event per cited source (Open WebUI shows the citation badges and the source list), then the message
        text, and checks itself that the chat belongs to `user_id` and that the message was queued by the pipe for this
        job (no admin key is involved: since Open WebUI 0.12 an admin cannot write into other users' chats). Retried 3
        times; the outcome is stored in jobs.delivery (a failed delivery is tried again later, see _sweep_deliveries).
        The pipe returns the report on the next message anyway.
        """
        s = self.settings
        if not (
            s.owui_delivery_url
            and s.owui_delivery_secret
            and job.get("chat_id")
            and job.get("message_id")
            and job.get("user_id")
        ):
            await self.db.execute("update jobs set delivery='skipped' where id=:i", i=job["id"])
            logger.info("delivery skipped", job=job["id"], reason="no delivery endpoint/secret or job without chat")
            return
        body = {
            "chat_id": job["chat_id"],
            "message_id": job["message_id"],
            "job_id": job["id"],
            "user_id": job.get("user_id") or "",
            "content": content,
            "sources": [e["data"] for e in owui_source_events(sources or [])],
        }
        outcome = "failed"
        for attempt in range(3):
            try:
                async with self.http_factory() as http:
                    r = await http.post(
                        s.owui_delivery_url, json=body, headers={"Authorization": f"Bearer {s.owui_delivery_secret}"}
                    )
                if r.status_code == 200:
                    outcome = "ok"
                    break
                outcome = f"failed: HTTP {r.status_code} {r.text[:200]}"
                if r.status_code in (400, 401, 403, 404, 413):  # not going to change by waiting
                    break
            except httpx.HTTPError as e:
                outcome = f"failed: {type(e).__name__}: {e}"[:300]
            if attempt < 2:
                await asyncio.sleep(self.retry_delay * (attempt + 1))
        if outcome == "ok":
            logger.info("delivery ok", job=job["id"], chat=job["chat_id"], sources=len(sources or []))
        else:
            logger.error("delivery failed", job=job["id"], chat=job["chat_id"], outcome=outcome)
        await self.db.execute("update jobs set delivery=:d where id=:i", d=outcome[:300], i=job["id"])


def new_id() -> str:
    """Return a new 12-character job or draft id."""
    return uuid.uuid4().hex[:12]
