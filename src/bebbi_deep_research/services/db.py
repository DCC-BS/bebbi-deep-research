"""Async database layer (SQLAlchemy Core with plain SQL): Postgres in production, SQLite for development."""

from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

SCHEMA = [
    """create table if not exists drafts (id text primary key, task text, preset text, depth text, plan text,
       created double precision, own_sources text)""",
    """create table if not exists jobs (id text primary key, draft text, task text, preset text, depth text, plan text,
       user_id text, chat_id text, message_id text, run_when text, status text, created double precision,
       started double precision, finished double precision, events text, report text, stats text, evidence text,
       error text, heartbeat double precision, delivery text, own_sources text)""",
    "create index if not exists jobs_status_idx on jobs (status, created)",
]


# columns added after the first release: `create table if not exists` does not touch existing tables
MIGRATIONS = [
    ("drafts", "own_sources", "text"),
    ("jobs", "own_sources", "text"),
    ("drafts", "user_id", "text"),
    ("jobs", "owner", "text"),
    ("jobs", "attempts", "integer"),
    ("jobs", "delivery_tries", "integer"),
]
INIT_LOCK = 727274  # pg_advisory_xact_lock key: several pods starting at once must not race on the schema


class DB:
    """Async database access (Postgres in production, SQLite for development and tests)."""

    def __init__(self, url: str):
        """Create the engine (connections open on first use).

        Args:
            url: SQLAlchemy async URL (``postgresql+asyncpg://`` or ``sqlite+aiosqlite://``).
        """
        kw = {"pool_pre_ping": True}
        if not url.startswith("sqlite"):
            kw.update(pool_size=5, max_overflow=5)
        self.url = url
        if url.startswith("sqlite") and ":memory:" not in url:
            Path(url.split(":///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
        self.engine: AsyncEngine = create_async_engine(url, **kw)

    async def init(self) -> None:
        """Create the tables and add columns of later versions (Postgres: under an advisory lock)."""
        async with self.engine.begin() as con:
            pg = con.dialect.name == "postgresql"
            if pg:
                await con.execute(text(f"select pg_advisory_xact_lock({INIT_LOCK})"))  # released at commit
            for stmt in SCHEMA:
                await con.execute(text(stmt))
            for table, column, kind in MIGRATIONS:
                if pg:
                    await con.execute(text(f"alter table {table} add column if not exists {column} {kind}"))
                    continue
                have = await con.run_sync(lambda c, t=table: {col["name"] for col in inspect(c).get_columns(t)})
                if column not in have:
                    await con.execute(text(f"alter table {table} add column {column} {kind}"))

    async def run(self, sql: str, **params) -> list[dict]:
        """Run one statement in its own transaction; returns rows as dicts (empty for writes)."""
        async with self.engine.begin() as con:
            res = await con.execute(text(sql), params)
            return [dict(r._mapping) for r in res.fetchall()] if res.returns_rows else []

    async def execute(self, sql: str, **params) -> int:
        """Run a write statement; returns the number of affected rows."""
        async with self.engine.begin() as con:
            return (await con.execute(text(sql), params)).rowcount

    async def ping(self) -> None:
        """Run a trivial query (raises if the database is unreachable)."""
        await self.run("select 1")

    async def close(self) -> None:
        """Close all connections."""
        await self.engine.dispose()
