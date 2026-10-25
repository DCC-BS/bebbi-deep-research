"""Retention: file texts leave the database when a job ends; drafts and finished jobs are deleted after their time."""

import json
import time

from .conftest import FakeAgent, running, submit, utc_window, wait_status
from .test_own_service import own_payload

DAY = 86400.0


async def _own_sources(app, job_id):
    rows = await app.state.container.db().run("select own_sources from jobs where id=:i", i=job_id)
    return json.loads(rows[0]["own_sources"] or "null")


async def test_failed_job_keeps_no_file_texts(tmp_path):
    FakeAgent.fail = True
    async with running(tmp_path, night_window=utc_window(True)) as (c, app):
        job = await submit(c, own_sources=own_payload())
        await wait_status(c, job["job_id"], {"error"})
        stored = await _own_sources(app, job["job_id"])
        assert stored["files"] == [
            {
                "id": "f1",
                "name": "merkblatt.pdf",
                "chars": len(("Frist 28 Tage. " * 50).strip()),
                "date": "2026-10-09",
                "url": "http://owui/api/v1/files/f1/content",
                "cut": False,
            }
        ]
        assert (await c.get(f"/jobs/{job['job_id']}")).json()["own_sources"]["files"][0]["name"] == "merkblatt.pdf"


async def test_queued_job_keeps_its_texts_until_it_ran(tmp_path):
    async with running(tmp_path, night_window=utc_window(False)) as (c, app):
        job = await submit(c, depth="standard", when="night", own_sources=own_payload())
        assert (await _own_sources(app, job["job_id"]))["files"][0]["text"].startswith("Frist 28 Tage")


async def test_purge_deletes_old_drafts_and_finished_jobs_and_old_texts(tmp_path):
    async with running(tmp_path, night_window=utc_window(False), job_retention_days=30) as (c, app):
        db, runner, now = app.state.container.db(), app.state.container.job_runner(), time.time()
        texts = json.dumps({"files": [{"id": "f", "name": "a.pdf", "text": "Geheim", "chars": 6}], "collections": []})
        for jid, status, finished, created in (
            ("old_done", "done", now - 31 * DAY, now - 31 * DAY),
            ("old_error", "error", now - 40 * DAY, now - 40 * DAY),
            ("new_done", "done", now - 29 * DAY, now - 29 * DAY),
            ("old_queued", "queued", None, now - 400 * DAY),  # not finished: never deleted by retention
        ):
            await db.execute(
                "insert into jobs (id, status, finished, created, own_sources) values (:i, :s, :f, :c, :o)",
                i=jid,
                s=status,
                f=finished,
                c=created,
                o=texts,
            )
        for did, created in (("d_old", now - 15 * DAY), ("d_new", now - 13 * DAY)):
            await db.execute("insert into drafts (id, created) values (:i, :c)", i=did, c=created)
        await runner.purge(now)
        jobs = {r["id"]: r for r in await db.run("select id, own_sources from jobs")}
        assert set(jobs) == {"new_done", "old_queued"}
        assert json.loads(jobs["new_done"]["own_sources"])["files"] == [{"id": "f", "name": "a.pdf", "chars": 6}]
        assert "Geheim" in jobs["old_queued"]["own_sources"]  # the run still needs it
        assert [r["id"] for r in await db.run("select id from drafts")] == ["d_new"]


async def test_indexes_exist(tmp_path):
    async with running(tmp_path, night_window=utc_window(False)) as (c, app):
        db = app.state.container.db()
        if db.url.startswith("sqlite"):
            rows = await db.run("select name from sqlite_master where type='index'")
        else:
            rows = await db.run("select indexname as name from pg_indexes where tablename in ('jobs','drafts')")
        names = {r["name"] for r in rows}
        assert {"jobs_user_idx", "jobs_message_idx", "jobs_draft_idx", "drafts_created_idx"} <= names
