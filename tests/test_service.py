import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from service.db import DB
from service.jobs import in_window
from service.settings import Settings, normalize_db_url

from .conftest import PLAN, FakeAgent, running, submit, utc_window, wait_status


# ------------------------------------------------------------------ probes, auth
async def test_probes_and_auth(tmp_path):
    async with running(tmp_path) as (c, _):
        assert (await c.get("/healthz")).json() == {"status": "ok"}
        assert (await c.get("/readyz")).status_code == 200
        assert (await httpx.AsyncClient(transport=c._transport, base_url="http://t").post("/clarify", json={"task": "x"})).status_code == 401
        assert (await c.get("/metrics")).text.count("research_jobs{") == 5


async def test_readyz_reports_missing_config(tmp_path):
    from service.app import create_app
    s = Settings.from_env({"RA_DATABASE_URL": f"sqlite:///{tmp_path}/x.db"})
    app = create_app(s)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get("/healthz")).status_code == 200            # liveness stays green
            r = await c.get("/readyz")
            assert r.status_code == 503 and "RA_TOKEN is not set" in r.json()["problems"]
            assert (await c.post("/clarify", json={"task": "x"}, headers={"Authorization": "Bearer "})).status_code == 401


# ---------------------------------------------------------------- chat turns
async def test_clarify_plan_edit(tmp_path):
    async with running(tmp_path) as (c, _):
        assert (await c.post("/clarify", json={"task": "x"})).json()["questions"]
        d = (await c.post("/plans", json={"task": "x", "preset": "grossrat", "depth": "quick"})).json()
        assert d["plan"]["title"] == PLAN["title"]
        e = (await c.post(f"/plans/{d['draft_id']}/edit", json={"instruction": "mehr"})).json()
        assert e["plan"]["title"] == "Edited: mehr"
        assert (await c.post("/plans/nope/edit", json={"instruction": "x"})).status_code == 404
        assert (await c.post("/plans", json={"task": "x", "preset": "nope"})).status_code == 400
        assert {p["name"] for p in (await c.get("/presets")).json()} >= {"grossrat", "web_survey"}


# --------------------------------------------------------------- job lifecycle
async def test_job_lifecycle_and_delivery(tmp_path):
    posted = []

    def handler(request: httpx.Request):
        posted.append((str(request.url), request.headers["authorization"], json.loads(request.content)))
        return httpx.Response(200, json=request.url.path.endswith("/event"))

    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: E731
    async with running(tmp_path, http_factory=factory, night_window=utc_window(True),
                       owui_url="http://owui", owui_api_key="k") as (c, _):
        job = await submit(c, chat_id="chat1", message_id="msg1")
        assert job["when"] == "now"
        j = await wait_status(c, job["job_id"], {"done"})
        assert j["report"].startswith("# Bericht") and j["stats"]["usage"]["calls"] == 3
        assert j["events"][-1]["text"] == "Recherchiere"
        for _ in range(40):                       # delivery follows the status change
            j = (await c.get(f"/jobs/{job['job_id']}")).json()
            if j["delivery"]:
                break
            await asyncio.sleep(0.05)
        assert j["delivery"] == "ok"
        # one source event per source (in the order of the numbers), then the message text
        base = "http://owui/api/v1/chats/chat1/messages/msg1"
        assert [(u, a) for u, a, _ in posted] == [(base + "/event", "Bearer k")] * 2 + [(base, "Bearer k")]
        events = [b for _, _, b in posted[:2]]
        assert [e["type"] for e in events] == ["source", "source"]
        assert [e["data"]["metadata"][0]["source"] for e in events] == [s["url"] for s in j["sources"]]
        assert posted[2][2] == {"content": j["ui_report"]} and "\n1. Vorstoss 23.5001" in j["ui_report"]
        assert j["owui_events"] == events
        assert "cards" in (await c.get(f"/jobs/{job['job_id']}/evidence")).json()
        assert 'research_jobs{status="done"} 1' in (await c.get("/metrics")).text


async def test_delivery_failure_is_recorded_and_report_stays(tmp_path):
    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))  # noqa: E731
    app_kw = dict(http_factory=factory, night_window=utc_window(True), owui_url="http://owui", owui_api_key="k")
    async with running(tmp_path, **app_kw) as (c, app):
        app.state.runner.retry_delay = 0
        job = await submit(c, chat_id="chat1", message_id="msg1")
        for _ in range(60):
            j = (await c.get(f"/jobs/{job['job_id']}")).json()
            if j["delivery"]:
                break
            await asyncio.sleep(0.05)
        assert j["status"] == "done" and j["report"] and j["delivery"].startswith("failed: HTTP 500")


async def test_no_delivery_without_chat(tmp_path):
    async with running(tmp_path, night_window=utc_window(True), owui_url="http://owui", owui_api_key="k") as (c, _):
        job = await submit(c)                      # no chat_id / message_id (API client such as n8n)
        await wait_status(c, job["job_id"], {"done"})
        for _ in range(40):
            if (await c.get(f"/jobs/{job['job_id']}")).json()["delivery"]:
                break
            await asyncio.sleep(0.05)
        assert (await c.get(f"/jobs/{job['job_id']}")).json()["delivery"] == "skipped"


async def test_failed_job(tmp_path):
    FakeAgent.fail = True
    async with running(tmp_path, night_window=utc_window(True)) as (c, _):
        job = await submit(c)
        j = await wait_status(c, job["job_id"], {"error"})
        assert "llm down" in j["error"]


async def test_llm_errors_are_reported_in_german(tmp_path, monkeypatch):
    import openai

    req = httpx.Request("POST", "http://llm/v1/chat/completions")
    err = openai.NotFoundError("model nonexistent not found", response=httpx.Response(404, request=req), body=None)

    async def broken(self, *a, **k):
        raise err

    monkeypatch.setattr(FakeAgent, "clarify", broken)
    monkeypatch.setattr(FakeAgent, "run", broken)
    async with running(tmp_path, night_window=utc_window(True)) as (c, _):
        r = await c.post("/clarify", json={"task": "x"}, )
        assert r.status_code == 502 and "Modellname" in r.json()["detail"]
        job = await submit(c)
        j = await wait_status(c, job["job_id"], {"error"})
        assert j["error"].startswith("Das Sprachmodell lehnt die Anfrage ab")


# ------------------------------------------------- night window and daytime rule
def test_in_window():
    s = Settings.from_env({"RA_TIMEZONE": "UTC", "RA_NIGHT_WINDOW": "20:00-05:00"})
    at = lambda h, m=0: datetime(2026, 10, 9, h, m, tzinfo=ZoneInfo("UTC"))  # noqa: E731
    assert in_window(s, at(23)) and in_window(s, at(4, 59)) and in_window(s, at(20))
    assert not in_window(s, at(5)) and not in_window(s, at(12)) and not in_window(s, at(19, 59))
    s.night_window = "08:00-17:00"
    assert in_window(s, at(8)) and not in_window(s, at(17))


async def test_daytime_rule(tmp_path):
    """Outside the window only quick runs of a focus-engine preset start; everything else waits for the night."""
    async with running(tmp_path, night_window=utc_window(False)) as (c, _):
        quick = await submit(c, "grossrat", "quick", "now")
        assert quick["when"] == "now"
        standard = await submit(c, "grossrat", "standard", "now")
        survey = await submit(c, "web_survey", "quick", "now")
        night = await submit(c, "grossrat", "quick", "night")
        assert standard["when"] == survey["when"] == night["when"] == "night"
        await wait_status(c, quick["job_id"], {"done"})
        await asyncio.sleep(0.4)
        for j in (standard, survey, night):
            assert (await c.get(f"/jobs/{j['job_id']}")).json()["status"] == "queued"
        assert [r[0] for r in FakeAgent.runs] == [quick["job_id"]]


async def test_day_workers_limit(tmp_path):
    FakeAgent.block = True
    async with running(tmp_path, night_window=utc_window(False), day_workers=1) as (c, _):
        a = await submit(c, when="now")
        b = await submit(c, when="now")
        await wait_status(c, a["job_id"], {"running"})
        await asyncio.sleep(0.4)
        assert (await c.get(f"/jobs/{b['job_id']}")).json()["status"] == "queued"


async def test_night_jobs_start_inside_window(tmp_path):
    async with running(tmp_path, night_window=utc_window(True)) as (c, _):
        job = await submit(c, "web_survey", "deep", "night")
        await wait_status(c, job["job_id"], {"done"})


# ------------------------------------------------------------------- quota
async def test_quota_default_off_and_enforced(tmp_path):
    async with running(tmp_path, night_window=utc_window(False)) as (c, _):
        for _ in range(3):                                  # default: no quota
            await submit(c, when="night")
    async with running(tmp_path / "q", night_window=utc_window(False), quota_per_day=1) as (c, _):
        await submit(c, when="night")
        d = (await c.post("/plans", json={"task": "x", "preset": "grossrat", "depth": "quick"})).json()
        r = await c.post("/jobs", json={"draft_id": d["draft_id"], "user_id": "u1"})
        assert r.status_code == 429


# ---------------------------------------------------- restart and shutdown
async def test_shutdown_requeues_running_job_and_restart_runs_it(tmp_path):
    FakeAgent.block = True
    async with running(tmp_path, night_window=utc_window(True)) as (c, app):
        db_url = app.state.db.url
        job = await submit(c)
        await wait_status(c, job["job_id"], {"running"})
    # the app is down now (lifespan exit cancelled the job): the row is queued again
    db = DB(db_url)
    assert (await db.run("select status from jobs"))[0]["status"] == "queued"
    await db.close()
    FakeAgent.block = False
    async with running(tmp_path, night_window=utc_window(True), keep_db=True) as (c, _):
        j = await wait_status(c, job["job_id"], {"done"})
        assert j["report"]


async def test_stale_job_of_dead_replica_is_requeued(tmp_path):
    async with running(tmp_path, night_window=utc_window(False), stale_seconds=1) as (c, app):
        job = await submit(c, when="night")
        await app.state.db.execute("update jobs set status='running', heartbeat=1 where id=:i", i=job["job_id"])
        await app.state.runner.tick()
        assert (await c.get(f"/jobs/{job['job_id']}")).json()["status"] == "queued"


async def test_claim_is_atomic(tmp_path):
    async with running(tmp_path, night_window=utc_window(False)) as (c, app):
        job = await submit(c, when="night")
        db = app.state.db
        sql = "update jobs set status='starting' where id=:i and status='queued'"
        assert await db.execute(sql, i=job["job_id"]) == 1
        assert await db.execute(sql, i=job["job_id"]) == 0


# ------------------------------------------------------------------- db, settings
async def test_db_layer_sqlite(tmp_path):
    db = DB(f"sqlite+aiosqlite:///{tmp_path}/d.db")
    await db.init()
    await db.init()                                         # idempotent
    await db.ping()
    await db.execute("insert into drafts (id, task, created) values (:i, :t, :c)", i="a", t="x", c=1.5)
    assert (await db.run("select * from drafts"))[0]["task"] == "x"
    await db.close()


def test_settings_parsing():
    assert normalize_db_url("postgresql://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert normalize_db_url("sqlite:///x.db") == "sqlite+aiosqlite:///x.db"
    s = Settings.from_env({"RA_LLM_EXTRA_BODY_THINK": '{"chat_template_kwargs": {"enable_thinking": true}}',
                           "RA_WORKERS": "x", "RA_NIGHT_WINDOW": "bogus"})
    assert s.extra_body_think["chat_template_kwargs"]["enable_thinking"] is True
    assert s.quota_per_day == 0 and s.day_workers == 1 and s.night_window == "bogus"
    assert any("RA_WORKERS" in p for p in s.problems()) and any("RA_NIGHT_WINDOW" in p for p in s.problems())
    assert Settings.from_env({"RA_LLM_EXTRA_BODY_FAST": "[1]"}).parse_errors


def test_llm_extra_body_per_role():
    from research_agent import LLM
    llm = LLM("http://x/v1", "k", extra_body={"think": {"chat_template_kwargs": {"enable_thinking": True}}})
    assert llm._kw("think") == {"extra_body": {"chat_template_kwargs": {"enable_thinking": True}}}
    assert llm._kw("fast") == {} and LLM("http://x/v1", "k")._kw("think") == {}


# ------------------------------------------------------- sources and Word export
async def test_job_result_has_structured_sources_and_docx(tmp_path):
    async with running(tmp_path, night_window=utc_window(True)) as (c, _):
        job = await submit(c, chat_id="chat1", message_id="msg1")
        j = await wait_status(c, job["job_id"], {"done"})
        s1, s2 = j["sources"]
        assert (s1["n"], s1["title"], s1["url"], s1["stand"], s1["fetched"], s1["kind"]) == (
            1, "Vorstoss 23.5001: Stadtklima", "https://grosserrat.bs.ch/geschaefte/23.5001", "2023-05-01", "2026-10-09", "mcp:grossrat")
        assert s1["excerpt"].startswith("Der Regierungsrat wird beauftragt") and s1["excerpt_kind"] == "quote"
        assert s2["stand"] == "" and s2["excerpt"].startswith("Die Frist beträgt 30 Tage") and s2["kind"] == "web"
        assert j["report"].count("## Quellen") == 1 and j["docx"] == f"/jobs/{job['job_id']}/report.docx"   # report text unchanged
        r = await c.get(j["docx"])
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats")
        assert 'filename="Bebbi-Deep-Research_Bericht_' in r.headers["content-disposition"]
        from io import BytesIO

        from docx import Document
        doc = Document(BytesIO(r.content))
        assert any("Der Grosse Rat hat den Vorstoss" in p.text for p in doc.paragraphs)
        assert (await c.get(j["docx"], headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await c.get("/jobs/nope/report.docx")).status_code == 404
        found = await c.get("/jobs/lookup", params={"chat_id": "chat1", "message_id": "msg1", "user_id": "u1"})
        assert found.json() == {"job_id": job["job_id"], "status": "done"}
        assert (await c.get("/jobs/lookup", params={"chat_id": "chat1", "message_id": "msg1", "user_id": "other"})).status_code == 404


async def test_docx_only_for_finished_jobs(tmp_path):
    from .conftest import FakeAgent
    FakeAgent.block = True
    async with running(tmp_path, night_window=utc_window(True)) as (c, _):
        job = await submit(c)
        await wait_status(c, job["job_id"], {"running"})
        assert (await c.get(f"/jobs/{job['job_id']}/report.docx")).status_code == 409
        assert (await c.get(f"/jobs/{job['job_id']}")).json()["sources"] == []


async def test_source_delivery_is_not_repeated_on_retry(tmp_path):
    """A failing text step is retried without sending the source events again (Open WebUI would store them twice)."""
    calls = []

    def handler(request: httpx.Request):
        if request.method == "GET":
            return httpx.Response(404)
        calls.append("event" if request.url.path.endswith("/event") else "text")
        if calls[-1] == "text" and calls.count("text") == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=True)

    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: E731
    async with running(tmp_path, http_factory=factory, night_window=utc_window(True), owui_url="http://owui", owui_api_key="k") as (c, app):
        app.state.runner.retry_delay = 0
        job = await submit(c, chat_id="chat1", message_id="msg1")
        for _ in range(60):
            j = (await c.get(f"/jobs/{job['job_id']}")).json()
            if j["delivery"]:
                break
            await asyncio.sleep(0.05)
        assert j["delivery"] == "ok" and calls == ["event", "event", "text", "text"]


async def test_sources_already_on_the_message_are_not_delivered_again(tmp_path):
    """"jetzt" jobs: the pipe returns the report itself and emits the sources; the service must not store them a second time."""
    posted = []

    def handler(request: httpx.Request):
        if request.method == "GET":
            return httpx.Response(200, json={"chat": {"history": {"messages": {"msg1": {"sources": [{"source": {"name": "x"}}]}}}}})
        posted.append(request.url.path)
        return httpx.Response(200, json=True)

    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: E731
    async with running(tmp_path, http_factory=factory, night_window=utc_window(True), owui_url="http://owui", owui_api_key="k") as (c, _):
        job = await submit(c, chat_id="chat1", message_id="msg1")
        for _ in range(60):
            j = (await c.get(f"/jobs/{job['job_id']}")).json()
            if j["delivery"]:
                break
            await asyncio.sleep(0.05)
        assert j["delivery"] == "ok" and posted == ["/api/v1/chats/chat1/messages/msg1"]
