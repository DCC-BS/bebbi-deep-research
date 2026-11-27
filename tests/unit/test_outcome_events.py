"""Usage events at the end of a run: research.done and research.failed (docs/operations.md, "Monitoring")."""

import asyncio

from structlog.contextvars import get_contextvars

from .conftest import DELIVERY, FakeAgent, running, submit, utc_window, wait_status


class Recorder:
    def __init__(self):
        self.events: list[tuple[str, str | None, dict]] = []

    def log_event(self, action, user_id=None, **fields):
        assert all(isinstance(v, str | int | float | bool) for v in fields.values()), fields  # flat (logging contract)
        self.events.append((action, user_id, fields))


async def wait_event(rec: Recorder, action: str) -> dict:
    for _ in range(100):  # the event follows the delivery, which follows the status change
        for a, user, fields in rec.events:
            if a == action:
                return {"user": user, **fields}
        await asyncio.sleep(0.02)
    raise AssertionError(f"no {action} event, got {[a for a, _, _ in rec.events]}")


def recorder(app) -> Recorder:
    rec = Recorder()
    app.state.container.job_runner().usage = rec
    return rec


async def test_done_event_carries_preset_outcome_runtime_and_tokens(tmp_path):
    async with running(tmp_path, night_window=utc_window(True)) as (c, app):
        rec = recorder(app)
        job = await submit(c, preset="grossrat", depth="quick")
        await wait_status(c, job["job_id"], {"done"})
        e = await wait_event(rec, "research.done")
    assert e["user"] == "u1" and e["job_id"] == job["job_id"]
    assert (e["preset"], e["depth"], e["delivery"]) == ("grossrat", "quick", "skipped")
    assert (e["llm_calls"], e["prompt_tokens"], e["completion_tokens"]) == (3, 10, 5)
    assert e["runtime_s"] >= 0 and e["queued_s"] >= 0 and e["daytime"] is True  # a quick focus run takes a daytime slot
    assert e["sources"] >= 0 and e["own_files"] == 0 and e["own_collections"] == 0
    assert isinstance(e["mcp_servers"], str) and e["mcp_count"] >= 1 and "style" not in e
    assert [a for a, _, _ in rec.events] == ["research.done"]


async def test_mixed_preset_is_split_into_its_choices(tmp_path):
    async with running(tmp_path, night_window=utc_window(True)) as (c, app):
        rec = recorder(app)
        mix = {"web": False, "mcp": ["data-bs", "grossrat"], "own": "prefer", "style": "kompakt"}
        name = (await c.post("/options/resolve", json=mix)).json()["preset"]
        assert name.startswith("mix:")
        job = await submit(c, preset=name)
        await wait_status(c, job["job_id"], {"done"})
        e = await wait_event(rec, "research.done")
    assert e["preset"] == name and e["style"] == mix["style"]
    assert e["web"] is False and e["own_mode"] == "prefer" and e["mcp_servers"] == "data-bs+grossrat"
    assert e["mcp_count"] == 2


async def test_delivered_job_reports_the_delivery(tmp_path):
    import httpx2

    factory = lambda: httpx2.AsyncClient(  # noqa: E731
        transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json={"ok": True}))
    )
    async with running(tmp_path, http_factory=factory, night_window=utc_window(True), **DELIVERY) as (c, app):
        rec = recorder(app)
        job = await submit(c, chat_id="chat1", message_id="msg1")
        e = await wait_event(rec, "research.done")
    assert e["job_id"] == job["job_id"] and e["delivery"] == "ok"


async def test_failed_event_names_the_error_type(tmp_path):
    FakeAgent.fail = True
    async with running(tmp_path, night_window=utc_window(True)) as (c, app):
        rec = recorder(app)
        job = await submit(c)
        await wait_status(c, job["job_id"], {"error"})
        e = await wait_event(rec, "research.failed")
    assert e["job_id"] == job["job_id"] and (e["reason"], e["error_type"]) == ("error", "RuntimeError")
    assert "research.done" not in [a for a, _, _ in rec.events]


async def test_given_up_job_logs_a_failed_event_and_no_pseudonym_leaks(tmp_path):
    from bebbi_deep_research.services.jobs import MAX_ATTEMPTS

    async with running(tmp_path, night_window=utc_window(False), stale_seconds=1) as (c, app):
        rec = recorder(app)
        job = await submit(c, when="night")
        await app.state.container.db().execute(
            "update jobs set status='running', heartbeat=1, attempts=:a where id=:i", a=MAX_ATTEMPTS, i=job["job_id"]
        )
        await app.state.container.job_runner().tick()
        e = await wait_event(rec, "research.failed")
        assert "pseudonym_id" not in get_contextvars()
    assert (e["reason"], e["preset"]) == ("interrupted", "grossrat")


async def test_a_broken_usage_logger_does_not_change_the_job(tmp_path):
    class Broken:
        def log_event(self, *a, **kw):
            raise RuntimeError("telemetry down")

    async with running(tmp_path, night_window=utc_window(True)) as (c, app):
        app.state.container.job_runner().usage = Broken()
        job = await submit(c)
        assert (await wait_status(c, job["job_id"], {"done"}))["status"] == "done"
