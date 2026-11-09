"""``serve`` starts every task (API, polling, worker, sweeper, reminders); SIGTERM stops them."""

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
from alfred.reminders.tools import ReminderTools
from alfred.web.tools import WebTools
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
        ("http://ai.lan", "gemma4:26b-a4b-it-qat"),
        ("http://ai.lan", "small"),
        ("http://ragman.lan", "nomic-embed-text:v1.5"),
    ]
    assert clients[2].kwargs == {
        "document_prefix": "search_document: ",
        "query_prefix": "search_query: ",
    }
    assert all(c.closed for c in clients)
    assert isinstance(agents[0]["memory"], Memory)
    assert agents[0]["brains"].names == ["gemma4:26b-a4b-it-qat"]


async def test_web_wiring_and_client_closed(
    database_url: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    session = PollingSession()
    monkeypatch.setattr(alfred.app, "Bot", lambda token: Bot(TOKEN, session=session))
    sweeper = SweeperProbe()
    monkeypatch.setattr(Memory, "run_sweeper", sweeper.run)
    built: list[WebTools] = []
    closed: list[WebTools] = []
    real_from_config = WebTools.from_config.__func__  # type: ignore[attr-defined]
    real_aclose = WebTools.aclose

    def from_config(cls: type[WebTools], config: Any) -> WebTools:
        tools = real_from_config(cls, config)
        built.append(tools)
        return tools

    async def aclose(self: WebTools) -> None:
        closed.append(self)
        await real_aclose(self)

    monkeypatch.setattr(WebTools, "from_config", classmethod(from_config))
    monkeypatch.setattr(WebTools, "aclose", aclose)
    agents: list[dict[str, Any]] = []
    real_agent = alfred.app.Agent

    def record_agent(*args: Any, **kwargs: Any) -> Any:
        agents.append(kwargs)
        return real_agent(*args, **kwargs)

    monkeypatch.setattr(alfred.app, "Agent", record_agent)
    config = serve_config(database_url, free_port())
    with caplog.at_level("INFO", logger="alfred.app"):
        task = asyncio.create_task(alfred.app.serve(config))
        await asyncio.wait_for(sweeper.started.wait(), 5)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, 10)

    assert len(built) == 1 and agents[0]["web"] is built[0]
    assert closed == built
    search = built[0]._search  # one client for search and fetch
    assert search._http is built[0]._fetcher._http
    assert search._url == "http://oracle.lan/search"
    assert "web search at http://oracle.lan" in caplog.text


async def test_reminder_wiring(
    database_url: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    session = PollingSession()
    monkeypatch.setattr(alfred.app, "Bot", lambda token: Bot(TOKEN, session=session))
    sweeper = SweeperProbe()
    monkeypatch.setattr(Memory, "run_sweeper", sweeper.run)
    loops: list[tuple[Any, float]] = []
    loop_started = asyncio.Event()
    loop_stopped = asyncio.Event()

    async def probe_loop(store: Any, signal_: asyncio.Event, interval_s: float) -> None:
        loops.append((store, interval_s))
        loop_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            loop_stopped.set()

    monkeypatch.setattr(alfred.app, "run_due_loop", probe_loop)
    agents: list[dict[str, Any]] = []
    real_agent = alfred.app.Agent

    def record_agent(*args: Any, **kwargs: Any) -> Any:
        agents.append(kwargs)
        return real_agent(*args, **kwargs)

    monkeypatch.setattr(alfred.app, "Agent", record_agent)
    config = serve_config(database_url, free_port())
    config = type(config)(
        **{
            **config.__dict__,
            "reminders": config.reminders.model_copy(update={"check_interval_s": 10}),
        }
    )
    with caplog.at_level("INFO", logger="alfred.app"):
        task = asyncio.create_task(alfred.app.serve(config))
        await asyncio.wait_for(loop_started.wait(), 5)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, 10)  # a clean shutdown

    assert loop_stopped.is_set()  # cancelled on shutdown, like memory
    tools = agents[0]["reminders"]
    assert isinstance(tools, ReminderTools)
    assert loops == [(tools.store, 10)]
    assert "reminders checked every 10s" in caplog.text


async def test_reminder_loop_failure_stops_serve(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = PollingSession()
    monkeypatch.setattr(alfred.app, "Bot", lambda token: Bot(TOKEN, session=session))
    monkeypatch.setattr(Memory, "run_sweeper", SweeperProbe().run)

    async def broken_loop(*args: Any) -> None:
        raise RuntimeError("loop broke")

    monkeypatch.setattr(alfred.app, "run_due_loop", broken_loop)
    with pytest.raises(RuntimeError, match="reminders stopped unexpectedly: RuntimeError"):
        await asyncio.wait_for(alfred.app.serve(serve_config(database_url, free_port())), 10)
