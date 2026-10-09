import asyncio
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from research_agent import Result
from service.app import create_app
from service.settings import Settings

TOKEN = "test-token"
H = {"Authorization": f"Bearer {TOKEN}"}
PLAN = {"title": "T", "task_core": "core", "language": "de", "parts": [{"id": "p1", "title": "Teil", "goal": "g"}],
        "sub_questions": [{"id": "q1", "part": "p1", "text": "Frage?", "intent": "primary"}], "must_name": [], "constraints": []}


REPORT = """# Bericht

## Teil

Der Grosse Rat hat den Vorstoss überwiesen [1]. Die Frist beträgt 30 Tage [2].

| Jahr | Anzahl |
|---|---|
| 2023 | 4 [2] |

## Quellen

[1] Vorstoss 23.5001: Stadtklima. https://grosserrat.bs.ch/geschaefte/23.5001 (Stand 2023-05-01; abgerufen am 2026-10-09)
[2] Kantonsverfassung, § 5: https://gesetzessammlung.bs.ch/app/de/texts_of_law/111.100 (abgerufen am 2026-10-09)
"""
LEDGER = {"cards": [
    {"id": "E1", "url": "https://grosserrat.bs.ch/geschaefte/23.5001", "title": "Vorstoss 23.5001: Stadtklima", "quote": "Der Regierungsrat wird beauftragt, ein Stadtklima-Konzept vorzulegen.",
     "finding": "f", "source_kind": "mcp:grossrat", "fetched_at": "2026-10-09", "date": "2023-05-01"},
    {"id": "E2", "url": "https://gesetzessammlung.bs.ch/app/de/texts_of_law/111.100", "title": "Kantonsverfassung, § 5", "quote": "kurz",
     "finding": "Die Frist beträgt 30 Tage nach Eingang.", "source_kind": "web", "fetched_at": "2026-10-09", "date": ""}]}


class FakeAgent:
    """Stands in for ResearchAgent: no LLM, no search. `block` makes run() wait until cancelled."""
    runs: list = []
    block = False
    fail = False

    def __init__(self, job_id, on_event=None):
        self.job_id, self.on_event = job_id, on_event

    async def clarify(self, task):
        return {"questions": ["Welcher Zeitraum?"], "assumptions": ["letzte 5 Jahre"]}

    async def plan(self, task, preset, budget):
        return PLAN

    async def edit_plan(self, plan, instruction, task, preset, budget):
        return {**plan, "title": "Edited: " + instruction}

    async def run(self, task, preset, depth, plan=None, requirements=""):
        FakeAgent.runs.append((self.job_id, preset, depth))
        if self.on_event:
            await self.on_event("status", "Recherchiere")
        if FakeAgent.block:
            await asyncio.sleep(3600)
        if FakeAgent.fail:
            raise RuntimeError("llm down")
        return Result(report=REPORT, plan=plan, ledger=LEDGER,
                      usage={"calls": 3, "prompt_tokens": 10, "completion_tokens": 5, "by_step": {}}, stats={"cards": 1})


def make_settings(tmp_path, **kw) -> Settings:
    env = {"RA_TOKEN": TOKEN, "RA_LLM_BASE_URL": "http://llm/v1", "RA_SEARCH_URL": "http://s/search",
           "RA_FIRECRAWL_URL": "http://fc", "RA_DATABASE_URL": f"sqlite:///{tmp_path}/t.db", "RA_POLL_SECONDS": "0.05",
           "RA_TIMEZONE": "UTC"}      # windows in tests are built from UTC time
    if os.environ.get("RA_TEST_DATABASE_URL"):       # run the suite against Postgres: tests are isolated by a schema per test
        env["RA_DATABASE_URL"] = os.environ["RA_TEST_DATABASE_URL"]
    s = Settings.from_env(env)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def utc_window(inside: bool) -> str:
    now = datetime.now(ZoneInfo("UTC"))
    a, b = (now - timedelta(hours=1), now + timedelta(hours=1)) if inside else (now + timedelta(hours=2), now + timedelta(hours=3))
    return f"{a:%H:%M}-{b:%H:%M}"


@pytest.fixture(autouse=True)
def reset_fake():
    FakeAgent.runs, FakeAgent.block, FakeAgent.fail = [], False, False


@asynccontextmanager
async def running(tmp_path, http_factory=None, keep_db=False, **kw):
    """App with lifespan (DB, worker loop) and an httpx client; yields (client, app)."""
    app = create_app(make_settings(tmp_path, **kw), agent_factory=lambda jid, on_event=None: FakeAgent(jid, on_event),
                     http_factory=http_factory)
    if os.environ.get("RA_TEST_DATABASE_URL") and not keep_db:
        await app.state.db.run("drop table if exists jobs")
        await app.state.db.run("drop table if exists drafts")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t", headers=H) as c:
            yield c, app


async def wait_status(c, job_id, want, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        j = (await c.get(f"/jobs/{job_id}")).json()
        if j["status"] in want:
            return j
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} did not reach {want}, last: {j['status']}")


async def submit(c, preset="grossrat", depth="quick", when="now", **extra):
    d = (await c.post("/plans", json={"task": "Frage", "preset": preset, "depth": depth})).json()
    r = await c.post("/jobs", json={"draft_id": d["draft_id"], "user_id": "u1", "when": when, **extra})
    assert r.status_code == 200, r.text
    return r.json()
