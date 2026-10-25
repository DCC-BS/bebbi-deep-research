"""Cancelling a job: POST /jobs/{id}/cancel, the run stops, the chat is told; the pipe's "abbrechen"."""

import asyncio
import json

import httpx

from .conftest import DELIVERY, FakeAgent, running, submit, utc_window, wait_status
from .test_own_service import own_payload
from .test_pipe import _job_history, load_pipe


def _delivery_recorder():
    posted = []

    def handler(request: httpx.Request):
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    return posted, lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _until(cond, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def test_queued_job_is_cancelled_and_the_chat_is_told(tmp_path):
    posted, factory = _delivery_recorder()
    async with running(tmp_path, http_factory=factory, night_window=utc_window(False), **DELIVERY) as (c, app):
        job = await submit(c, depth="standard", when="night", chat_id="c1", message_id="m1", own_sources=own_payload())
        jid = job["job_id"]
        assert (await c.post(f"/jobs/{jid}/cancel", params={"user_id": "u2"})).status_code == 404  # not the owner
        r = await c.post(f"/jobs/{jid}/cancel", params={"user_id": "u1"})
        assert r.status_code == 200 and r.json() == {"job_id": jid, "status": "cancelled"}
        await _until(lambda: posted)
        assert posted[0]["job_id"] == jid and posted[0]["content"].startswith("Die Recherche wurde abgebrochen.")
        j = (await c.get(f"/jobs/{jid}")).json()
        assert j["status"] == "cancelled" and j["finished"] and not j["report"]
        stored = (await app.state.container.db().run("select own_sources from jobs where id=:i", i=jid))[0]
        assert "Frist" not in stored["own_sources"]  # texts leave with the end of the job
        again = await c.post(f"/jobs/{jid}/cancel")
        assert again.status_code == 409 and "bereits beendet" in again.json()["debugMessage"]
        assert (await c.post("/jobs/nope/cancel")).status_code == 404
        assert 'research_jobs{status="cancelled"} 1' in (await c.get("/metrics")).text


async def test_running_job_stops_and_stays_cancelled(tmp_path):
    FakeAgent.gate = asyncio.Event()  # the run waits until the test releases it
    async with running(tmp_path, night_window=utc_window(True)) as (c, app):
        job = await submit(c)
        await wait_status(c, job["job_id"], {"running"})
        runner = app.state.container.job_runner()
        task = runner.tasks[job["job_id"]]
        assert (await c.post(f"/jobs/{job['job_id']}/cancel")).status_code == 200
        await asyncio.wait([task], timeout=5)
        assert task.cancelled()
        await asyncio.sleep(0.2)  # a few worker ticks: it is not queued again
        row = (await app.state.container.db().run("select status, report, owner from jobs"))[0]
        assert row == {"status": "cancelled", "report": None, "owner": None}


async def test_run_on_this_replica_stops_when_another_replica_cancelled_the_job(tmp_path):
    FakeAgent.gate = asyncio.Event()
    async with running(tmp_path, night_window=utc_window(True)) as (c, app):
        job = await submit(c)
        await wait_status(c, job["job_id"], {"running"})
        task = app.state.container.job_runner().tasks[job["job_id"]]
        await app.state.container.db().execute("update jobs set status='cancelled', owner=null")
        await asyncio.wait([task], timeout=5)
        assert task.cancelled()


def test_pipe_cancels_on_abbrechen():
    mod = load_pipe()
    pipe = mod.Pipe()
    calls = []

    async def api(method, path, body=None):
        calls.append((method, path))
        return {"job_id": "j1", "status": "cancelled"}

    pipe._api = api
    out = asyncio.run(pipe.pipe(_job_history(mod, "Abbrechen!"), {"id": "u"}))
    assert out == mod.CANCELLED and "research-agent" not in out
    assert calls == [("POST", "/jobs/j1/cancel?user_id=u")]


def test_pipe_shows_a_cancelled_job_and_offers_cancel_while_it_runs():
    mod = load_pipe()
    pipe = mod.Pipe()
    jobs = {"status": "cancelled", "events": []}

    async def api(method, path, body=None):
        return jobs

    pipe._api = api
    assert asyncio.run(pipe.pipe(_job_history(mod), {"id": "u"})) == mod.CANCELLED
    jobs["status"] = "queued"
    assert "**abbrechen**" in asyncio.run(pipe.pipe(_job_history(mod), {"id": "u"}))


def test_pipe_shows_the_report_when_the_job_ended_before_the_cancel():
    mod = load_pipe()
    pipe = mod.Pipe()

    async def api(method, path, body=None):
        if path.endswith("/cancel?user_id=u"):
            raise mod.UserError("Die Recherche ist bereits beendet und kann nicht mehr abgebrochen werden.")
        return {"status": "done", "events": [], "report": "# Bericht"}

    pipe._api = api
    assert asyncio.run(pipe.pipe(_job_history(mod, "abbrechen"), {"id": "u"})).startswith("# Bericht")
