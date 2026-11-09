"""Shared fixtures.

One Postgres container per test session (testcontainers), migrated with
``alfred migrate``. Each test that uses the database gets its own async engine
and starts from empty tables.

The image is a pgvector build of Postgres 17. The ``vector`` extension needs a
superuser, so, as in the deployment's init script, it is created before
migrating rather than by the migration (design D2).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alfred.db.engine import make_engine, make_session_factory
from alfred.web.fetch import make_http_client

# Ryuk (the reaper container) needs a privileged socket that rootless podman
# doesn't give it; containers are stopped by the fixture instead.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

TABLES = "turn_tool_calls, entries, conversations, queue_items, reminders"
POSTGRES_IMAGE = "docker.io/pgvector/pgvector:0.8.7-pg17-trixie"


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """A libpq-style URL (``postgresql://``) of a fresh, unmigrated database with ``vector``."""
    import psycopg
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(POSTGRES_IMAGE, driver=None) as pg:
        url = pg.get_connection_url()
        # The container's user is a superuser, standing in for the init script.
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        yield url


@pytest.fixture(scope="session")
def database_url(postgres_url: str) -> str:
    """The migrated test database, as alfred's psycopg URL."""
    from alfred.cli import main

    mp = pytest.MonkeyPatch()
    mp.setenv("ALFRED_DATABASE_URL", postgres_url)
    try:
        assert main(["migrate"]) == 0
    finally:
        mp.undo()
    return postgres_url.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(database_url)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    yield engine
    await engine.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(engine)


@pytest.fixture
async def web_http() -> AsyncIterator[httpx.AsyncClient]:
    async with make_http_client() as client:
        yield client
