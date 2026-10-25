"""The API contract between the pipe and the service: version header, unknown fields, the real pipe against the app."""

import importlib.util

import httpx

from bebbi_deep_research.routers.contract import API_VERSION, HEADER

from .conftest import running, submit, utc_window, wait_status
from .test_pipe import ROOT, load_pipe


async def test_other_major_version_is_refused_in_german(tmp_path):
    async with running(tmp_path, night_window=utc_window(False)) as (c, app):
        r = await c.post("/clarify", json={"task": "x"}, headers={HEADER: str(API_VERSION + 1)})
        assert r.status_code == 409 and "Bitte wenden Sie sich an den Betrieb" in r.json()["debugMessage"]
        assert (await c.post("/clarify", json={"task": "x"}, headers={HEADER: "eins"})).status_code == 400
        ok = await c.post("/clarify", json={"task": "x"}, headers={HEADER: f"{API_VERSION}.3"})
        assert ok.status_code == 200 and ok.headers[HEADER] == str(API_VERSION)
        assert (await c.get("/jobs/nope", headers={HEADER: "99"})).status_code == 409


async def test_unknown_fields_are_refused(tmp_path):
    async with running(tmp_path, night_window=utc_window(False)) as (c, app):
        assert (await c.post("/clarify", json={"task": "x", "suggest_presets": True})).status_code == 422
        assert (await c.post("/plans", json={"task": "x", "preset": "grossrat", "tiefe": "quick"})).status_code == 422
        own = {"files": [{"id": "f", "name": "a", "text": "t", "secret": 1}]}
        assert (await c.post("/clarify", json={"task": "x", "own_sources": own})).status_code == 422
        d = (await c.post("/plans", json={"task": "x", "preset": "grossrat", "depth": "quick"})).json()
        assert (await c.post(f"/plans/{d['draft_id']}/edit", json={"instruction": "m", "x": 1})).status_code == 422
        r = await c.post("/jobs", json={"draft_id": d["draft_id"], "user_id": "u", "priority": 1})
        assert r.status_code == 422


async def test_report_read_without_user_and_version_still_works(tmp_path):
    """n8n and scripts read reports with the token only: no version header, no user_id."""
    async with running(tmp_path, night_window=utc_window(True)) as (c, app):
        job = await submit(c)
        await wait_status(c, job["job_id"], {"done"})
        r = await c.get(f"/jobs/{job['job_id']}")
        assert r.status_code == 200 and r.json()["report"]
        assert (await c.get(f"/jobs/{job['job_id']}/report.docx")).status_code == 200


REAL_CLIENT = httpx.AsyncClient


def _route_to(monkeypatch, app):
    """Send the HTTP calls of the pipe and the action (both use `httpx.AsyncClient(timeout=...)`) to the app."""
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=httpx.ASGITransport(app=app), **kw))


def _answer(index):
    async def call(event):
        if event["type"] == "request:user_input":
            qid = event["data"]["questions"][0]["id"]
            return {"status": "answered", "answers": {qid: {"type": "option", "option_index": index}}}
        return None

    return call


async def test_the_real_pipe_talks_to_the_real_service(tmp_path, monkeypatch):
    """The pipe's requests pass the service's models (extra fields forbidden) and its version check."""
    async with running(tmp_path, night_window=utc_window(False)) as (c, app):
        mod = load_pipe()
        _route_to(monkeypatch, app)
        pipe = mod.Pipe()
        pipe.valves.SERVICE_URL, pipe.valves.SERVICE_TOKEN = "http://service", "test-token"
        events = []

        async def emit(e):
            events.append(e)

        # model "Deep Research": the preset card (first option = the suggestion), then the clarifying questions
        first = {"model": "bebbi_deep_research.auto", "messages": [{"role": "user", "content": "Frage schnell"}]}
        out = await pipe.pipe(first, {"id": "u1"}, __event_emitter__=emit, __event_call__=_answer(0))
        assert "Welcher Zeitraum?" in out
        # the answer: plan, card "Freigeben", job queued for the night
        second = {
            "model": "bebbi_deep_research.auto",
            "messages": [
                *first["messages"],
                {"role": "assistant", "content": out},
                {"role": "user", "content": "2020"},
            ],
        }
        out = await pipe.pipe(
            second,
            {"id": "u1"},
            __event_emitter__=emit,
            __event_call__=_answer(0),
            __chat_id__="c1",
            __message_id__="m1",
        )
        assert out == ""  # the queued text went out as a replace event
        queued = events[-1]["data"]["content"]
        assert "Nachtfenster" in queued
        job_id = mod._state([{"role": "assistant", "content": queued}, {}])["job"]
        row = (await app.state.container.db().run("select user_id, chat_id, preset from jobs where id=:i", i=job_id))[0]
        assert row == {"user_id": "u1", "chat_id": "c1", "preset": "grossrat"}


async def test_pipe_of_another_version_shows_the_service_message(tmp_path, monkeypatch):
    async with running(tmp_path, night_window=utc_window(False)) as (c, app):
        mod = load_pipe()
        _route_to(monkeypatch, app)
        mod.API_VERSION = str(API_VERSION + 1)
        pipe = mod.Pipe()
        pipe.valves.SERVICE_URL, pipe.valves.SERVICE_TOKEN = "http://service", "test-token"
        body = {"model": "bebbi_deep_research.grossrat", "messages": [{"role": "user", "content": "Frage"}]}
        out = await pipe.pipe(body, {"id": "u1"})
        assert out.startswith("Deep Research in Open WebUI (Schnittstelle 2) passt nicht")


async def test_download_action_sends_the_version(tmp_path, monkeypatch):
    async with running(tmp_path, night_window=utc_window(True)) as (c, app):
        job = await submit(c, chat_id="c1", message_id="m1")
        await wait_status(c, job["job_id"], {"done"})
        spec = importlib.util.spec_from_file_location("dl_mod", ROOT / "pipe/deep_research_download.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _route_to(monkeypatch, app)
        mod.API_VERSION = "7"
        action = mod.Action()
        action.valves.SERVICE_URL, action.valves.SERVICE_TOKEN = "http://service", "test-token"
        toasts = []

        async def emit(e):
            toasts.append(e["data"])

        await action.action({"chat_id": "c1", "id": "m1"}, {"id": "u1"}, emit, __request__=object())
        assert toasts[-1]["type"] == "error" and "Schnittstelle 7" in toasts[-1]["content"]


def test_pipe_and_action_speak_the_service_version():
    assert load_pipe().API_VERSION == str(API_VERSION)
    spec = importlib.util.spec_from_file_location("dl_mod2", ROOT / "pipe/deep_research_download.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.API_VERSION == str(API_VERSION)
