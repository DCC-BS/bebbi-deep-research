"""Postgres-only behaviour of the database layer (needs RA_TEST_DATABASE_URL, a throwaway database).

docker run --rm -d --name bdr-pg -e POSTGRES_PASSWORD=x -p 55432:5432 postgres:16-alpine
RA_TEST_DATABASE_URL=postgresql://postgres:x@localhost:55432/postgres mise run integration
"""

import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest

from bebbi_deep_research.services.db import DB, MIGRATIONS
from bebbi_deep_research.services.jobs import JobRunner
from bebbi_deep_research.utils.app_config import normalize_db_url

URL = os.environ.get("RA_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="RA_TEST_DATABASE_URL is not set")

# the schema of the first release (before own sources, owners, attempts and delivery retries)
FIRST_RELEASE = [
    "create table drafts (id text primary key, task text, preset text, depth text, plan text, created double precision)",
    "create table jobs (id text primary key, draft text, task text, preset text, depth text, plan text, user_id text,"
    " chat_id text, message_id text, run_when text, status text, created double precision, started double precision,"
    " finished double precision, events text, report text, stats text, evidence text, error text,"
    " heartbeat double precision, delivery text)",
]


async def _fresh() -> DB:
    db = DB(normalize_db_url(URL))
    await db.run("drop table if exists jobs")
    await db.run("drop table if exists drafts")
    return db


async def _columns(db: DB, table: str) -> set[str]:
    rows = await db.run("select column_name from information_schema.columns where table_name=:t", t=table)
    return {r["column_name"] for r in rows}


async def test_init_of_several_pods_at_once_does_not_race():
    db = await _fresh()
    pods = [DB(normalize_db_url(URL)) for _ in range(4)]
    await asyncio.gather(*(p.init() for p in pods))
    for table, column, _ in MIGRATIONS:
        assert column in await _columns(db, table)
    await asyncio.gather(*(p.close() for p in pods), db.close())


async def test_database_of_the_first_release_gets_the_new_columns_and_keeps_its_rows():
    db = await _fresh()
    for stmt in FIRST_RELEASE:
        await db.run(stmt)
    await db.execute("insert into jobs (id, status, created) values ('old', 'done', 1.0)")
    await db.init()
    await db.init()  # idempotent
    for table, column, _ in MIGRATIONS:
        assert column in await _columns(db, table)
    rows = await db.run("select id, status, attempts, owner from jobs")
    assert rows == [{"id": "old", "status": "done", "attempts": None, "owner": None}]
    indexes = {
        r["indexname"] for r in await db.run("select indexname from pg_indexes where tablename in ('jobs','drafts')")
    }
    assert {"jobs_user_idx", "jobs_message_idx", "jobs_draft_idx", "drafts_created_idx"} <= indexes
    await db.close()


async def test_retention_on_postgres():
    db = await _fresh()
    await db.init()
    now, day = time.time(), 86400.0
    texts = json.dumps({"files": [{"id": "f", "name": "a.pdf", "text": "Geheim", "chars": 6}], "collections": []})
    for jid, status, finished in (("old", "done", now - 366 * day), ("new", "error", now - day), ("q", "queued", None)):
        await db.execute(
            "insert into jobs (id, status, finished, created, own_sources) values (:i, :s, :f, :c, :o)",
            i=jid,
            s=status,
            f=finished,
            c=now - 400 * day,
            o=texts,
        )
    await db.execute("insert into drafts (id, created) values ('d', :c)", c=now - 15 * day)
    runner = JobRunner(SimpleNamespace(job_retention_days=365), db, None, None)
    await runner.purge(now)
    jobs = {r["id"]: r["own_sources"] for r in await db.run("select id, own_sources from jobs")}
    assert set(jobs) == {"new", "q"} and "Geheim" not in jobs["new"] and "Geheim" in jobs["q"]
    assert await db.run("select id from drafts") == []
    await db.close()
