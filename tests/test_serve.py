"""``serve`` starts the API, polling, the worker and the sweeper, and stops cleanly on SIGTERM."""

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
from alfred.memory.service import Memory
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


class SweeperProbe:
    """Stands in for the sweeper, so the test never calls the real AI or ragman."""

    def __init__(self, fail: bool = False) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.fail = fail

    async def run(self) -> None:
        self.started.set()
        try:
            if self.fail:
                raise RuntimeError("sweeper broke")
            await asyncio.Event().wait()
        finally:
            self.stopped.set()


def serve_config(database_url: str, port: int):  # type: ignore[no-untyped-def]
    base = make_config()
    return type(base)(
        **{
            **base.__dict__,
            "server": ServerConfig(host="127.0.0.1", port=port),
            "database_url": SecretStr(database_url),
        }
    )


async def test_serve_runs_and_stops_on_sigterm(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = free_port()
    session = PollingSession()
    monkeypatch.setattr(alfred.app, "Bot", lambda token: Bot(TOKEN, session=session))
    sweeper = SweeperProbe()
    monkeypatch.setattr(Memory, "run_sweeper", sweeper.run)
    config = serve_config(database_url, port)
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
    await asyncio.wait_for(sweeper.started.wait(), 5)
    assert not task.done()

    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, 10)  # returns normally: a clean shutdown
    assert sweeper.stopped.is_set()


async def test_sweeper_failure_stops_serve(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = PollingSession()
    monkeypatch.setattr(alfred.app, "Bot", lambda token: Bot(TOKEN, session=session))
    monkeypatch.setattr(Memory, "run_sweeper", SweeperProbe(fail=True).run)
    with pytest.raises(RuntimeError, match="memory stopped unexpectedly: RuntimeError"):
        await asyncio.wait_for(alfred.app.serve(serve_config(database_url, free_port())), 10)


async def test_memory_wiring_and_clients_closed(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = PollingSession()
    monkeypatch.setattr(alfred.app, "Bot", lambda token: Bot(TOKEN, session=session))
    sweeper = SweeperProbe()
    monkeypatch.setattr(Memory, "run_sweeper", sweeper.run)
    clients: list[Any] = []

    class FakeClient:
        def __init__(self, base_url: str, model: str, timeout_s: float, **kwargs: Any) -> None:
            self.base_url, self.model, self.kwargs = base_url, model, kwargs
            self.closed = False
            clients.append(self)

        async def aclose(self) -> None:
            self.closed = True

    agents: list[dict[str, Any]] = []
    real_agent = alfred.app.Agent

    def record_agent(*args: Any, **kwargs: Any) -> Any:
        agents.append(kwargs)
        return real_agent(*args, **kwargs)

    monkeypatch.setattr(alfred.app, "ModelClient", FakeClient)
    monkeypatch.setattr(alfred.app, "EmbeddingClient", FakeClient)
    monkeypatch.setattr(alfred.app, "Agent", record_agent)
    config = serve_config(database_url, free_port())
    config = type(config)(
        **{**config.__dict__, "memory": config.memory.model_copy(update={"summary_model": "small"})}
    )
    task = asyncio.create_task(alfred.app.serve(config))
    await asyncio.wait_for(sweeper.started.wait(), 5)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, 10)

    assert [(c.base_url, c.model) for c in clients] == [
        ("http://ai.lan", "gemma4:12b-it-qat"),
        ("http://ai.lan", "small"),
        ("http://ragman.lan", "nomic-embed-text:v1.5"),
    ]
    assert clients[2].kwargs == {
        "document_prefix": "search_document: ",
        "query_prefix": "search_query: ",
    }
    assert all(c.closed for c in clients)
    assert isinstance(agents[0]["memory"], Memory)
