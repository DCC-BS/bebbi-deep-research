import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from dcc_backend_common.config import AppConfigError

from bebbi_deep_research.services.db import DB
from bebbi_deep_research.services.policy import in_window
from bebbi_deep_research.utils.app_config import AppConfig, normalize_db_url

from .conftest import DELIVERY, PLAN, FakeAgent, running, submit, utc_window, wait_status


# ------------------------------------------------------------------ probes, auth
async def test_probes_and_auth(tmp_path):
    async with running(tmp_path) as (c, _):
        assert (await c.get("/health/liveness")).json()["status"] == "up"
        assert (await c.get("/health/startup")).status_code == 200
        ready = await c.get("/health/readiness")  # the database only: the test LLM endpoint does not exist
        assert ready.status_code == 200 and ready.json()["checks"] == {"database": "healthy"}
        deps = await c.get("/health/dependencies")  # reported, but never a reason to take the pod out
        assert deps.status_code == 200 and deps.json()["status"] == "degraded"
        assert deps.json()["checks"]["llm"] != "healthy" and "open-webui" not in deps.json()["checks"]
        anonymous = httpx.AsyncClient(transport=c._transport, base_url="http://t")
        r = await anonymous.post("/clarify", json={"task": "x"})
        assert r.status_code == 401 and r.json()["errorId"] == "authentication_failed"
        assert (await c.get("/metrics")).text.count("research_jobs{") == 6


async def test_readiness_fails_without_the_database(tmp_path):
    async with running(tmp_path) as (c, app):

        async def down():
            raise ConnectionRefusedError("db down")

        app.state.container.db().ping = down
        r = await c.get("/health/readiness")
        assert r.status_code == 503 and r.json()["checks"] == {"database": "ConnectionRefusedError"}


async def test_dependencies_report_each_service(tmp_path, monkeypatch):
    def handler(request):
        return httpx.Response(200 if request.url.path == "/v1/models" else 503)

    real = httpx.AsyncClient
    async with running(tmp_path, owui_url="http://owui") as (c, _):
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
        r = await c.get("/health/dependencies")
        assert r.json() == {"status": "degraded", "checks": {"llm": "healthy", "open-webui": "status 503"}}


def test_describe_error_names_the_cause_without_internal_hosts():
    from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior

    from bebbi_deep_research.services.policy import describe_error

    assert describe_error(ModelAPIError("gemma", "Connection error to http://vllm.ai.svc:8000")).startswith(
        "Das Sprachmodell ist nicht erreichbar"
    )
    assert "vllm.ai.svc" not in describe_error(ModelAPIError("gemma", "Connection error to http://vllm.ai.svc:8000"))
    assert describe_error(UnexpectedModelBehavior("empty")).startswith("Das Sprachmodell hat keine verwertbare Antwort")


def test_missing_required_configuration_stops_the_start():
    with pytest.raises(AppConfigError, match="RA_TOKEN"):
        AppConfig.from_env({"HMAC_SECRET": "x", "RA_LLM_BASE_URL": "u", "RA_SEARCH_URL": "u", "RA_FIRECRAWL_URL": "u"})


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
        return httpx.Response(200, json={"ok": True})

    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: E731
    async with running(
        tmp_path,
        http_factory=factory,
        night_window=utc_window(True),
        owui_url="http://owui",
        owui_api_key="admin-key",
        **DELIVERY,
    ) as (c, _):
        job = await submit(c, chat_id="chat1", message_id="msg1")
        assert job["when"] == "now"
        j = await wait_status(c, job["job_id"], {"done"})
        assert j["report"].startswith("# Bericht") and j["stats"]["usage"]["calls"] == 3
        assert j["events"][-1]["text"] == "Recherchiere"
        for _ in range(40):  # delivery follows the status change
            j = (await c.get(f"/jobs/{job['job_id']}")).json()
            if j["delivery"]:
                break
            await asyncio.sleep(0.05)
        assert j["delivery"] == "ok"
        # one call to the delivery endpoint with the shared secret (not the admin key): chat, message, job, user,
        # the text and one source event payload per source, in the order of the numbers
        assert len(posted) == 1
        url, auth, body = posted[0]
        assert (url, auth) == ("http://owui/api/v1/dcc/deep-research/deliver", "Bearer shared")
        assert (body["chat_id"], body["message_id"], body["job_id"], body["user_id"]) == (
            "chat1",
            "msg1",
            job["job_id"],
            "u1",
        )
        assert body["content"] == j["ui_report"] and "\n1. Vorstoss 23.5001" in j["ui_report"]
        assert body["sources"] == [e["data"] for e in j["owui_events"]]
        assert [m["metadata"][0]["source"] for m in body["sources"]] == [s["url"] for s in j["sources"]]
        assert "cards" in (await c.get(f"/jobs/{job['job_id']}/evidence")).json()
        assert 'research_jobs{status="done"} 1' in (await c.get("/metrics")).text


async def test_delivery_never_uses_the_admin_key_or_the_chat_api(tmp_path):
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: E731
    async with running(
        tmp_path,
        http_factory=factory,
        night_window=utc_window(True),
        owui_url="http://owui",
        owui_api_key="admin-key",
        **DELIVERY,
    ) as (c, _):
        job = await submit(c, chat_id="chat1", message_id="msg1")
        await wait_status(c, job["job_id"], {"done"})
        for _ in range(40):
            if (await c.get(f"/jobs/{job['job_id']}")).json()["delivery"]:
                break
            await asyncio.sleep(0.05)
    assert seen and all("/api/v1/chats" not in str(r.url) for r in seen)
    assert all("admin-key" not in r.headers.get("authorization", "") for r in seen)


async def test_delivery_failure_is_recorded_and_report_stays(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500, text="boom")

    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: E731
    app_kw = dict(http_factory=factory, night_window=utc_window(True), **DELIVERY)
    async with running(tmp_path, **app_kw) as (c, app):
        app.state.container.job_runner().retry_delay = 0
        job = await submit(c, chat_id="chat1", message_id="msg1")
        for _ in range(60):
            j = (await c.get(f"/jobs/{job['job_id']}")).json()
            if j["delivery"]:
                break
            await asyncio.sleep(0.05)
        assert j["status"] == "done" and j["report"] and j["delivery"].startswith("failed: HTTP 500")
        assert len(calls) == 3  # server errors are retried


async def test_refusal_by_the_endpoint_is_not_retried(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403, json={"detail": "message is not queued for this job"})

    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: E731
    async with running(tmp_path, http_factory=factory, night_window=utc_window(True), **DELIVERY) as (c, app):
        app.state.container.job_runner().retry_delay = 0
        job = await submit(c, chat_id="chat1", message_id="msg1")
        for _ in range(60):
            j = (await c.get(f"/jobs/{job['job_id']}")).json()
            if j["delivery"]:
                break
            await asyncio.sleep(0.05)
        assert j["delivery"].startswith("failed: HTTP 403") and "not queued" in j["delivery"]
        assert len(calls) == 1


async def test_no_delivery_without_secret(tmp_path):
    calls = []
    factory = lambda: httpx.AsyncClient(  # noqa: E731
        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200))
    )
    async with running(
        tmp_path,
        http_factory=factory,
        night_window=utc_window(True),
        owui_api_key="k",
        owui_delivery_url=DELIVERY["owui_delivery_url"],
    ) as (c, _):
        job = await submit(c, chat_id="chat1", message_id="msg1")  # URL and admin key set, delivery secret not
        await wait_status(c, job["job_id"], {"done"})
        for _ in range(40):
            if (await c.get(f"/jobs/{job['job_id']}")).json()["delivery"]:
                break
            await asyncio.sleep(0.05)
        assert (await c.get(f"/jobs/{job['job_id']}")).json()["delivery"] == "skipped"
    assert calls == []


def test_delivery_url_defaults_to_the_route_of_the_image():
    env = {
        "RA_TOKEN": "t",
        "HMAC_SECRET": "h",
        "RA_LLM_BASE_URL": "u",
        "RA_SEARCH_URL": "u",
        "RA_FIRECRAWL_URL": "u",
        "OWUI_URL": "http://open-webui:8080/",
        "OWUI_DELIVERY_SECRET": "s",
    }
    s = AppConfig.from_env(env)
    assert s.owui_delivery_url == "http://open-webui:8080/api/v1/dcc/deep-research/deliver"
    assert AppConfig.from_env({**env, "OWUI_DELIVERY_URL": "http://x/y/"}).owui_delivery_url == "http://x/y"
    assert AppConfig.from_env({k: v for k, v in env.items() if k != "OWUI_URL"}).owui_delivery_url == ""


async def test_no_delivery_without_chat(tmp_path):
    async with running(tmp_path, night_window=utc_window(True), **DELIVERY) as (c, _):
        job = await submit(c)  # no chat_id / message_id (API client such as n8n)
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
    from pydantic_ai.exceptions import ModelHTTPError

    err = ModelHTTPError(404, "nonexistent", body={"message": "model nonexistent not found"})

    async def broken(self, *a, **k):
        raise err

    monkeypatch.setattr(FakeAgent, "clarify", broken)
    monkeypatch.setattr(FakeAgent, "run", broken)
    async with running(tmp_path, night_window=utc_window(True)) as (c, _):
        r = await c.post(
            "/clarify",
            json={"task": "x"},
        )
        assert r.status_code == 502 and "Modellname" in r.json()["debugMessage"]
        job = await submit(c)
        j = await wait_status(c, job["job_id"], {"error"})
        assert j["error"].startswith("Das Sprachmodell lehnt die Anfrage ab")


# ------------------------------------------------- night window and daytime rule
def test_in_window():
    s = AppConfig.from_env({**REQUIRED, "RA_TIMEZONE": "UTC", "RA_NIGHT_WINDOW": "20:00-05:00"})
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
        for _ in range(3):  # default: no quota
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
        db_url = app.state.container.db().url
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
        await app.state.container.db().execute(
            "update jobs set status='running', heartbeat=1 where id=:i", i=job["job_id"]
        )
        await app.state.container.job_runner().tick()
        assert (await c.get(f"/jobs/{job['job_id']}")).json()["status"] == "queued"


async def test_claim_is_atomic(tmp_path):
    """Two runners (two replicas) tick on the same database: the real claim lets exactly one of them start the job."""
    from bebbi_deep_research.services.jobs import JobRunner

    FakeAgent.block = True
    async with running(tmp_path, night_window=utc_window(False)) as (c, app):
        job = await submit(c, when="night")
        s = app.state.container.config().model_copy(update={"night_window": utc_window(True)})
        a, b = (
            JobRunner(
                s,
                app.state.container.db(),
                app.state.container.presets(),
                lambda jid, on_event=None: FakeAgent(jid, on_event),
            )
            for _ in range(2)
        )
        await asyncio.gather(a.tick(), b.tick())
        assert len(a.tasks) + len(b.tasks) == 1
        row = (
            await app.state.container.db().run("select status, owner, attempts from jobs where id=:i", i=job["job_id"])
        )[0]
        assert row["status"] in ("starting", "running") and row["owner"] and row["attempts"] == 1
        await asyncio.gather(a.stop(), b.stop())


# ------------------------------------------------------------------- db, settings
async def test_db_layer_sqlite(tmp_path):
    db = DB(f"sqlite+aiosqlite:///{tmp_path}/d.db")
    await db.init()
    await db.init()  # idempotent
    await db.ping()
    await db.execute("insert into drafts (id, task, created) values (:i, :t, :c)", i="a", t="x", c=1.5)
    assert (await db.run("select * from drafts"))[0]["task"] == "x"
    await db.close()


REQUIRED = {
    "RA_TOKEN": "secret-token",
    "HMAC_SECRET": "h",
    "RA_LLM_BASE_URL": "u",
    "RA_SEARCH_URL": "u",
    "RA_FIRECRAWL_URL": "u",
}


def test_settings_parsing():
    assert normalize_db_url("postgresql://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert normalize_db_url("sqlite:///x.db") == "sqlite+aiosqlite:///x.db"
    s = AppConfig.from_env(
        {**REQUIRED, "RA_LLM_EXTRA_BODY_THINK": '{"chat_template_kwargs": {"enable_thinking": true}}'}
    )
    assert s.extra_body_think["chat_template_kwargs"]["enable_thinking"] is True
    assert s.quota_per_day == 0 and s.day_workers == 1 and s.night_window == "20:00-05:00"
    assert "secret-token" not in str(s) and "token=se**********" in str(s)  # secrets are masked in the log line
    for bad in (
        {"RA_WORKERS": "x"},
        {"RA_WORKERS": "0"},
        {"RA_NIGHT_WINDOW": "bogus"},
        {"RA_NIGHT_WINDOW": "00:00-00:00"},
        {"RA_TIMEZONE": "Mars/Base"},
    ):
        with pytest.raises(ValueError):
            AppConfig.from_env({**REQUIRED, **bad})
    with pytest.raises(AppConfigError):
        AppConfig.from_env({**REQUIRED, "RA_LLM_EXTRA_BODY_FAST": "[1]"})


# ------------------------------------------------------- sources and Word export
async def test_job_result_has_structured_sources_and_docx(tmp_path):
    async with running(tmp_path, night_window=utc_window(True)) as (c, _):
        job = await submit(c, chat_id="chat1", message_id="msg1")
        j = await wait_status(c, job["job_id"], {"done"})
        s1, s2 = j["sources"]
        assert (s1["n"], s1["title"], s1["url"], s1["stand"], s1["fetched"], s1["kind"]) == (
            1,
            "Vorstoss 23.5001: Stadtklima",
            "https://grosserrat.bs.ch/geschaefte/23.5001",
            "2023-05-01",
            "2026-10-09",
            "mcp:grossrat",
        )
        assert s1["excerpt"].startswith("Der Regierungsrat wird beauftragt") and s1["excerpt_kind"] == "quote"
        assert s2["stand"] == "" and s2["excerpt"].startswith("Die Frist beträgt 30 Tage") and s2["kind"] == "web"
        assert (
            j["report"].count("## Quellen") == 1 and j["docx"] == f"/jobs/{job['job_id']}/report.docx"
        )  # report text unchanged
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
        assert (
            await c.get("/jobs/lookup", params={"chat_id": "chat1", "message_id": "msg1", "user_id": "other"})
        ).status_code == 404


async def test_docx_only_for_finished_jobs(tmp_path):
    from .conftest import FakeAgent

    FakeAgent.block = True
    async with running(tmp_path, night_window=utc_window(True)) as (c, _):
        job = await submit(c)
        await wait_status(c, job["job_id"], {"running"})
        assert (await c.get(f"/jobs/{job['job_id']}/report.docx")).status_code == 409
        assert (await c.get(f"/jobs/{job['job_id']}")).json()["sources"] == []


async def test_no_delivery_without_url(tmp_path):
    calls = []
    factory = lambda: httpx.AsyncClient(  # noqa: E731
        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200))
    )
    async with running(
        tmp_path, http_factory=factory, night_window=utc_window(True), owui_delivery_secret="shared"
    ) as (c, _):
        job = await submit(c, chat_id="chat1", message_id="msg1")  # secret set, endpoint URL not
        await wait_status(c, job["job_id"], {"done"})
        for _ in range(40):
            if (await c.get(f"/jobs/{job['job_id']}")).json()["delivery"]:
                break
            await asyncio.sleep(0.05)
        assert (await c.get(f"/jobs/{job['job_id']}")).json()["delivery"] == "skipped"
    assert calls == []
