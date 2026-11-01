"""Shared fixtures.

One Postgres container per test session (testcontainers), migrated with
``alfred migrate``. Each test that uses the database gets its own async engine
and starts from empty tables.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alfred.db.engine import make_engine, make_session_factory

# Ryuk (the reaper container) needs a privileged socket that rootless podman
# doesn't give it; containers are stopped by the fixture instead.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

TABLES = "turn_tool_calls, entries, conversations, queue_items"


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """A libpq-style URL (``postgresql://``) of a fresh, unmigrated database."""
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("docker.io/library/postgres:17", driver=None) as pg:
        yield pg.get_connection_url()


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
