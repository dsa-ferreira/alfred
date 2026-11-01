"""``serve`` starts the API, polling and the worker, and stops cleanly on SIGTERM."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
from typing import Any

import httpx
import pytest
from aiogram import Bot
from aiogram.methods import DeleteWebhook, GetMe, GetUpdates
from aiogram.types import User
from pydantic import SecretStr

import alfred.app
from alfred.config import ServerConfig
from tests.helpers import make_config
from tests.telegram.fake_bot import TOKEN, FakeSession


class PollingSession(FakeSession):
    async def make_request(  # type: ignore[no-untyped-def]
        self,
        bot: Bot,
        method: Any,
        timeout: int | None = None,  # noqa: ASYNC109
    ):
        if isinstance(method, GetMe | GetUpdates | DeleteWebhook):
            self.requests.append(method)
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="alfred", username="alfred_bot")
        if isinstance(method, GetUpdates):
            await asyncio.sleep(0.05)
            return []
        if isinstance(method, DeleteWebhook):
            return True
        return await super().make_request(bot, method, timeout)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_serve_runs_and_stops_on_sigterm(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = free_port()
    session = PollingSession()
    monkeypatch.setattr(alfred.app, "Bot", lambda token: Bot(TOKEN, session=session))
    base = make_config()
    config = type(base)(
        **{
            **base.__dict__,
            "server": ServerConfig(host="127.0.0.1", port=port),
            "database_url": SecretStr(database_url),
        }
    )
    task = asyncio.create_task(alfred.app.serve(config))
    async with httpx.AsyncClient() as client:
        for _ in range(100):
            try:
                response = await client.get(f"http://127.0.0.1:{port}/healthz")
                break
            except httpx.ConnectError:
                await asyncio.sleep(0.05)
        else:
            pytest.fail("API never came up")
    assert response.status_code == 200
    for _ in range(100):
        if any(isinstance(r, GetUpdates) for r in session.requests):
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("Telegram polling never started")
    assert not task.done()

    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, 10)  # returns normally: a clean shutdown
