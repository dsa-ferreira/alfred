"""Orphan objects are deleted from the bucket after a day (design D10)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.attachments.sweeper import OrphanSweeper
from alfred.queue import Queue
from tests.agent.turn_helpers import Clock
from tests.helpers import FakeStore, image_file

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


async def put_at(store: FakeStore, clock: Clock, key: str, when: datetime) -> None:
    clock.now = when
    await store.put(key, b"x", "image/jpeg")


async def test_crash_between_upload_and_record(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock = Clock(NOW)
    store = FakeStore(now=clock)
    await put_at(store, clock, "attachments/orphan", NOW - timedelta(days=2))
    await put_at(store, clock, "attachments/kept", NOW - timedelta(days=2))
    await Queue(sessions).enqueue_owner("total?", None, [image_file("attachments/kept")])
    clock.now = NOW
    assert await OrphanSweeper(store, sessions, now=clock).sweep_once() == 1
    assert set(store.objects) == {"attachments/kept"}


async def test_fresh_orphan_is_kept(sessions: async_sessionmaker[AsyncSession]) -> None:
    clock = Clock(NOW)
    store = FakeStore(now=clock)
    await put_at(store, clock, "attachments/uploading", NOW - timedelta(hours=23))
    assert await OrphanSweeper(store, sessions, now=clock).sweep_once() == 0
    assert "attachments/uploading" in store.objects


async def test_objects_outside_the_prefix_are_left_alone(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock = Clock(NOW)
    store = FakeStore(now=clock)
    await put_at(store, clock, "backups/db.tar", NOW - timedelta(days=30))
    assert await OrphanSweeper(store, sessions, now=clock).sweep_once() == 0
    assert "backups/db.tar" in store.objects


async def test_store_error_is_survived(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock(NOW)
    store = FakeStore(now=clock)
    await put_at(store, clock, "attachments/orphan", NOW - timedelta(days=2))
    clock.now = NOW
    store.failing = True
    sweeper = OrphanSweeper(store, sessions, interval_s=0.01, now=clock)
    with caplog.at_level(logging.WARNING):
        task = asyncio.create_task(sweeper.run_forever())
        await asyncio.sleep(0.05)
        assert not task.done()  # failed rounds, still running
        store.failing = False
        for _ in range(100):
            if not store.objects:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert store.objects == {}
    assert "orphan sweep failed" in caplog.text
