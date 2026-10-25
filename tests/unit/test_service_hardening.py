"""Job lifecycle, ownership and input hardening of the service (review findings M1-M5, L1-L7, L9, L14)."""

import asyncio
import time

import httpx

from bebbi_deep_research.services.jobs import MAX_ATTEMPTS, describe_error
from bebbi_deep_research.utils.app_config import AppConfig

from .conftest import DELIVERY, FakeAgent, running, submit, utc_window, wait_status


async def row(app, job_id):
    return (await app.state.container.db().run("select * from jobs where id=:i", i=job_id))[0]


async def until(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if await cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


# ------------------------------------------------------------ M1: delivery is separate from the run
async def test_delivery_error_does_not_turn_done_into_error(tmp_path):
    async with running(tmp_path, night_window=utc_window(True)) as (c, app):

        async def boom(job, content, sources=None):
            raise RuntimeError("db hiccup in deliver")

        app.state.container.job_runner().deliver = boom
        job = await submit(c)
        await wait_status(c, job["job_id"], {"done"})
        await until(lambda: _delivery(app, job["job_id"]))
        r = await row(app, job["job_id"])
        assert r["status"] == "done" and r["report"] and r["delivery"].startswith("failed: RuntimeError")


async def _delivery(app, job_id):
    return (await row(app, job_id))["delivery"]


async def test_cancel_during_delivery_no_requeue(tmp_path):
    started = asyncio.Event()

    async def slow(request):
        started.set()
        await asyncio.sleep(3600)

    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(slow))  # noqa: E731
    async with running(tmp_path, http_factory=factory, night_window=utc_window(True), **DELIVERY) as (c, app):
        job = await submit(c, chat_id="chat1", message_id="msg1")
        await asyncio.wait_for(started.wait(), 5)
        await app.state.container.job_runner().stop()
        r = await row(app, job["job_id"])
        assert r["status"] == "done" and r["report"]  # not queued again: it would be researched a second time


async def test_failed_delivery_is_sweeped_later(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request):
        calls["n"] += 1
        return httpx.Response(500, text="x") if calls["n"] <= 3 else httpx.Response(200, json={"ok": True})

    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: E731
    async with running(tmp_path, http_factory=factory, night_window=utc_window(True), **DELIVERY) as (c, app):
        runner = app.state.container.job_runner()
        runner.retry_delay, runner.sweep_after = 0, 0
        job = await submit(c, chat_id="chat1", message_id="msg1")
        await wait_status(c, job["job_id"], {"done"})
        await until(lambda: _delivery(app, job["job_id"]))
        assert (await _delivery(app, job["job_id"])).startswith("failed")
        runner._last_sweep = 0
        await asyncio.sleep(0.01)
        await runner.tick()
        await until(lambda: _is(app, job["job_id"], "ok"))
        assert (await row(app, job["job_id"]))["delivery_tries"] == 1


async def _is(app, job_id, want):
    return (await _delivery(app, job_id)) == want


# ------------------------------------------------------------ M2: attempts
async def test_job_is_given_up_after_repeated_interruptions(tmp_path):
    async with running(tmp_path, night_window=utc_window(False)) as (c, app):
        job = await submit(c, when="night")
        await app.state.container.db().execute(
            "update jobs set status='running', attempts=:a, heartbeat=:h, owner='dead' where id=:i",
            a=MAX_ATTEMPTS,
            h=time.time() - 3600,
            i=job["job_id"],
        )
        await app.state.container.job_runner().tick()
        r = await row(app, job["job_id"])
        assert r["status"] == "error" and "unterbrochen" in r["error"]
        # below the limit the job goes back into the queue
        job2 = await submit(c, when="night")
        await app.state.container.db().execute(
            "update jobs set status='running', attempts=1, heartbeat=:h where id=:i",
            h=time.time() - 3600,
            i=job2["job_id"],
        )
        await app.state.container.job_runner().tick()
        assert (await row(app, job2["job_id"]))["status"] == "queued"


async def test_setup_failure_ends_in_error_not_a_dead_task(tmp_path):
    async with running(tmp_path, night_window=utc_window(True)) as (c, app):

        def broken(job_id, on_event=None):
            raise RuntimeError("factory broken")

        app.state.container.job_runner().make_agent = broken
        job = await submit(c)
        j = await wait_status(c, job["job_id"], {"error"})
        assert "Unerwarteter Fehler" in j["error"]


async def test_hung_run_is_stopped_by_the_job_timeout(tmp_path):
    """A hung LLM or MCP call keeps the heartbeat alive (worker tick), so only the timeout frees the worker."""

    class Hung(FakeAgent):
        async def run(self, *a, **kw):
            await asyncio.sleep(3600)

    async with running(tmp_path, night_window=utc_window(True), job_timeout_seconds=0.3) as (c, app):
        app.state.container.job_runner().make_agent = Hung
        job = await submit(c)
        j = await wait_status(c, job["job_id"], {"error"})
        assert "Zeitlimit" in j["error"]
        assert (await row(app, job["job_id"]))["owner"]  # stopped by this run, not re-queued as stale


# ------------------------------------------------------------ M3: progress write must not abort the run
async def test_progress_write_failure_does_not_abort_job(tmp_path):
    async with running(tmp_path, night_window=utc_window(True)) as (c, app):
        db = app.state.container.db()
        real = db.execute

        async def flaky(sql, **p):
            if "set events=" in sql:
                raise RuntimeError("db hiccup")
            return await real(sql, **p)

        db.execute = flaky
        job = await submit(c)
        await wait_status(c, job["job_id"], {"done"})


# ------------------------------------------------------------ M4: one job per draft, quota
async def test_submit_is_idempotent_per_draft(tmp_path):
    async with running(tmp_path, night_window=utc_window(False)) as (c, app):
        d = (await c.post("/plans", json={"task": "x", "preset": "grossrat", "depth": "quick"})).json()
        body = {"draft_id": d["draft_id"], "user_id": "u1", "when": "night"}
        a = (await c.post("/jobs", json=body)).json()
        b = (await c.post("/jobs", json=body)).json()
        assert a["job_id"] == b["job_id"]
        assert (await app.state.container.db().run("select count(*) n from jobs"))[0]["n"] == 1


async def test_quota_in_the_insert(tmp_path):
    async with running(tmp_path, night_window=utc_window(False), quota_per_day=1) as (c, _):
        await submit(c, when="night")
        d = (await c.post("/plans", json={"task": "x", "preset": "grossrat", "depth": "quick"})).json()
        r = await c.post("/jobs", json={"draft_id": d["draft_id"], "user_id": "u1"})
        assert r.status_code == 429
        r = await c.post("/jobs", json={"draft_id": d["draft_id"], "user_id": "u2"})
        assert r.status_code == 200


# ------------------------------------------------------------ M5: daytime rule when the job starts
async def test_deep_now_job_does_not_start_by_day(tmp_path):
    FakeAgent.block = True
    async with running(tmp_path, night_window=utc_window(True)) as (c, app):
        job = await submit(c, preset="verwaltung_web", depth="deep", when="now")  # accepted: the window is open
        assert job["when"] == "now"
        await app.state.container.job_runner().stop()  # nothing running; window closes below
        app.state.container.job_runner()._stopping = False
        await app.state.container.db().execute(
            "update jobs set status='queued', owner=null where id=:i", i=job["job_id"]
        )
        app.state.container.config().night_window = utc_window(False)
        await app.state.container.job_runner().tick()
        assert (await row(app, job["job_id"]))["status"] == "queued" and not FakeAgent.runs


async def test_running_night_job_does_not_take_the_daytime_slot(tmp_path):
    FakeAgent.gate = asyncio.Event()
    async with running(tmp_path, night_window=utc_window(True)) as (c, app):
        night = await submit(c, preset="verwaltung_web", depth="deep", when="night")
        await until(lambda: _running(app, night["job_id"]))
        app.state.container.config().night_window = utc_window(False)  # office hours; the night job is still running
        quick = await submit(c, preset="verwaltung_web", depth="quick", when="now")
        await until(lambda: _running(app, quick["job_id"]))
        assert len(FakeAgent.runs) == 2
        FakeAgent.gate.set()


async def _running(app, job_id):
    return (await row(app, job_id))["status"] in ("running", "done")


# ------------------------------------------------------------ L1: only the claiming run writes the final status
async def test_run_that_lost_the_job_does_not_overwrite(tmp_path):
    FakeAgent.gate = asyncio.Event()
    async with running(tmp_path, night_window=utc_window(True)) as (c, app):
        job = await submit(c)
        await until(lambda: _running(app, job["job_id"]))
        await app.state.container.db().execute(
            "update jobs set owner='someone-else' where id=:i", i=job["job_id"]
        )  # taken over
        FakeAgent.gate.set()
        await asyncio.sleep(0.4)
        r = await row(app, job["job_id"])
        assert r["status"] == "running" and not r["report"]


# ------------------------------------------------------------ L4, L5, L6, L7
async def test_non_ascii_authorization_is_401(tmp_path):
    async with running(tmp_path) as (c, _):
        r = await c.get("/presets", headers={"Authorization": "Bearer é".encode("latin-1")})
        assert r.status_code == 401


def test_settings_reject_an_empty_window_and_no_workers():
    import pytest

    required = {
        "RA_TOKEN": "t",
        "HMAC_SECRET": "h",
        "RA_LLM_BASE_URL": "u",
        "RA_SEARCH_URL": "u",
        "RA_FIRECRAWL_URL": "u",
    }
    with pytest.raises(ValueError, match="must differ"):
        AppConfig.from_env({**required, "RA_NIGHT_WINDOW": "00:00-00:00"})
    with pytest.raises(ValueError, match="workers"):
        AppConfig.from_env({**required, "RA_WORKERS": "0"})


async def test_size_limits(tmp_path):
    async with running(tmp_path) as (c, _):
        assert (await c.post("/plans", json={"task": "x" * 30_000})).status_code == 422
        assert (await c.post("/plans/abc/edit", json={"instruction": "x" * 20_000})).status_code == 422
        own = {"files": [{"id": str(i), "name": "n"} for i in range(60)]}
        assert (await c.post("/clarify", json={"task": "x", "own_sources": own})).status_code == 422


def test_error_detail_without_hosts():
    import openai

    e = openai.APIConnectionError(
        message="Connection to http://llm.internal.svc:8000/v1 failed, host llm.internal.svc 10.1.2.3:8000",
        request=httpx.Request("GET", "http://x"),
    )
    msg = describe_error(e)
    assert "nicht erreichbar" in msg and "llm.internal" not in msg and "10.1.2.3" not in msg and "http://" not in msg


# ------------------------------------------------------------ L9: owner checks
async def test_owner_checks(tmp_path):
    async with running(tmp_path, night_window=utc_window(True)) as (c, _):
        d = (await c.post("/plans", json={"task": "x", "preset": "grossrat", "depth": "quick", "user_id": "u1"})).json()
        did = d["draft_id"]
        assert (await c.post(f"/plans/{did}/edit", json={"instruction": "m", "user_id": "u2"})).status_code == 404
        assert (await c.post(f"/plans/{did}/edit", json={"instruction": "m", "user_id": "u1"})).status_code == 200
        assert (await c.post(f"/plans/{did}/edit", json={"instruction": "m"})).status_code == 200
        assert (await c.post("/jobs", json={"draft_id": did, "user_id": "u2"})).status_code == 404
        job = (await c.post("/jobs", json={"draft_id": did, "user_id": "u1", "when": "now"})).json()
        jid = job["job_id"]
        await wait_status(c, jid, {"done"})
        for path in (f"/jobs/{jid}", f"/jobs/{jid}/report.docx", f"/jobs/{jid}/evidence"):
            assert (await c.get(path, params={"user_id": "u2"})).status_code == 404
            assert (await c.get(path, params={"user_id": "u1"})).status_code == 200
            assert (await c.get(path)).status_code == 200  # without the parameter: unchanged (n8n)
        assert "user_id" not in (await c.get(f"/jobs/{jid}")).json()


# ------------------------------------------------------------ H1 (engine): the real factory with default settings
def test_real_agent_factory_with_empty_api_key():
    from bebbi_deep_research.engine import load_config
    from bebbi_deep_research.services.jobs import make_agent_factory

    s = AppConfig(token="t", hmac_secret="h", llm_base_url="http://llm/v1", search_url="u", firecrawl_url="u")
    agent = make_agent_factory(load_config(s.presets_path), s)("job1")
    assert agent is not None


def test_agent_factory_passes_the_embedding_endpoint():
    from bebbi_deep_research.engine import load_config
    from bebbi_deep_research.services.jobs import make_agent_factory

    env = {"RA_TOKEN": "t", "HMAC_SECRET": "h", "RA_LLM_BASE_URL": "http://llm/v1", "RA_LLM_API_KEY": "k-{job}"}
    env |= {"RA_SEARCH_URL": "u", "RA_FIRECRAWL_URL": "u"}
    s = AppConfig.from_env(env)
    assert s.llm_embed_base_url == "" and s.llm_embed_api_key == ""
    agent = make_agent_factory(load_config(s.presets_path), s)("job1")
    assert str(agent.llm._embed_client.base_url) == "http://llm/v1/" and agent.llm._embed_client.api_key == "k-job1"
    s = AppConfig.from_env(env | {"RA_LLM_EMBED_BASE_URL": "http://emb/v1", "RA_LLM_EMBED_API_KEY": "e-{job}"})
    assert "e-{job}" not in str(s)  # masked in the log line like the other keys
    agent = make_agent_factory(load_config(s.presets_path), s)("job2")
    assert str(agent.llm._embed_client.base_url) == "http://emb/v1/" and agent.llm._embed_client.api_key == "e-job2"
