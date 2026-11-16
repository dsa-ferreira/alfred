from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from aiogram.exceptions import TelegramNetworkError
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.prompts import _NO_REPLY
from alfred.agent.turn import TurnResult
from alfred.api import create_app
from alfred.config import Service, Users
from alfred.db.models import QueueItem
from alfred.history import History
from alfred.queue import Queue, Worker
from alfred.telegram.bot import TelegramOutbox
from tests.helpers import ANA, ANA_ID, DIOGO, FakeModel, make_agent, make_config, text
from tests.mcp.servers import in_process
from tests.telegram.fake_bot import make_bot

SERVICES = (
    Service("backup", SecretStr("tok-backup")),
    Service("disk-check", SecretStr("tok-disk")),
)
BACKUP = {"Authorization": "Bearer tok-backup"}
DISK = {"Authorization": "Bearer tok-disk"}
USERS = Users((DIOGO, ANA))


class Env:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.bot, self.telegram = make_bot()
        self.queue = Queue(sessions)
        self.history = History(sessions)
        self.outbox = TelegramOutbox(self.bot)
        self.app = create_app(SERVICES, USERS, self.queue, self.history, self.outbox)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://alfred"
        )

    async def items(self) -> list[QueueItem]:
        async with self.sessions() as session:
            return list(await session.scalars(select(QueueItem)))


@pytest.fixture
async def env(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[Env]:
    e = Env(sessions)
    yield e
    await e.client.aclose()


# --- Service authentication ----------------------------------------------------


async def test_valid_token_attributed(env: Env) -> None:
    response = await env.client.post("/notify", json={"text": "hi"}, headers=BACKUP)
    assert response.status_code == 200
    assert env.telegram.sent()[0].text == "[backup] hi"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer nope"},
        {"Authorization": "Basic tok-backup"},
        {"Authorization": "tok-backup"},
    ],
    ids=["missing", "empty", "empty-bearer", "unknown", "wrong-scheme", "no-scheme"],
)
@pytest.mark.parametrize("path", ["/notify", "/ask"])
async def test_rejected_tokens(env: Env, headers: dict[str, str], path: str) -> None:
    response = await env.client.post(path, json={"text": "x", "prompt": "x"}, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert env.telegram.requests == []
    assert await env.items() == []
    assert await env.history.recent("diogo", 30) == []


async def test_bad_token_with_bad_body_is_401(env: Env) -> None:
    response = await env.client.post("/notify", json={}, headers={"Authorization": "Bearer x"})
    assert response.status_code == 401


# --- Health endpoint -----------------------------------------------------------


async def test_health(env: Env) -> None:
    response = await env.client.get("/healthz")
    assert response.status_code == 200
    assert env.telegram.requests == []


# --- Direct notification -------------------------------------------------------


async def test_notify_recorded_as_notice(env: Env) -> None:
    response = await env.client.post(
        "/notify", json={"text": "Nightly backup failed"}, headers=BACKUP
    )
    assert response.status_code == 200
    (sent,) = env.telegram.sent()
    assert sent.text == "[backup] Nightly backup failed"
    (entry,) = await env.history.recent("diogo", 30)
    assert (entry.kind, entry.source, entry.text) == ("notice", "backup", "Nightly backup failed")


@pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": "   "}, {"text": None}])
async def test_notify_empty_text(env: Env, body: dict) -> None:
    response = await env.client.post("/notify", json=body, headers=BACKUP)
    assert response.status_code == 422
    assert env.telegram.requests == []


async def test_notify_while_ai_busy(env: Env) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    class BusyAgent:
        async def run(self, turn):  # type: ignore[no-untyped-def]
            started.set()
            await release.wait()
            return TurnResult("done", False, False)

    worker = Worker(
        env.queue, BusyAgent(), env.history, env.outbox, make_config().queue, users=USERS
    )
    await env.queue.enqueue_user("diogo", "long job")
    task = asyncio.create_task(worker._step())
    await asyncio.wait_for(started.wait(), 5)

    response = await asyncio.wait_for(
        env.client.post("/notify", json={"text": "hello"}, headers=BACKUP), 5
    )
    assert response.status_code == 200
    assert [m.text for m in env.telegram.sent()] == ["[backup] hello"]
    release.set()
    await task


# --- Notify delivery result ----------------------------------------------------


async def test_telegram_unreachable(env: Env) -> None:
    def fail(method) -> None:  # type: ignore[no-untyped-def]
        raise TelegramNetworkError(method, "unreachable")

    env.telegram.on_request = fail
    response = await env.client.post("/notify", json={"text": "hi"}, headers=BACKUP)
    assert response.status_code == 502
    assert await env.history.recent("diogo", 30) == []


# --- Ask the AI ----------------------------------------------------------------


async def test_ask_accepted(env: Env) -> None:
    response = await env.client.post(
        "/ask", json={"prompt": "/mnt/data is at 93%, was 80% yesterday"}, headers=DISK
    )
    assert response.status_code == 202
    (item,) = await env.items()
    assert response.json() == {"id": item.id}
    assert (item.kind, item.source, item.status) == ("ask", "disk-check", "pending")
    assert item.text == "/mnt/data is at 93%, was 80% yesterday"


@pytest.mark.parametrize("body", [{}, {"prompt": ""}, {"prompt": " \n"}])
async def test_ask_empty_prompt(env: Env, body: dict) -> None:
    response = await env.client.post("/ask", json=body, headers=DISK)
    assert response.status_code == 422
    assert await env.items() == []


# --- AI decides whether to notify ----------------------------------------------


async def run_ask(env: Env, answer: str) -> None:
    cfg = make_config()
    agent = make_agent(cfg, FakeModel([text(answer)]), env.history, env.sessions, in_process({}))
    worker = Worker(env.queue, agent, env.history, env.outbox, cfg.queue, users=USERS)
    await env.client.post("/ask", json={"prompt": "usage report"}, headers=DISK)
    while await worker._step():
        pass


async def test_nothing_worth_reporting(env: Env) -> None:
    await run_ask(env, _NO_REPLY)
    assert env.telegram.requests == []


async def test_worth_reporting(env: Env) -> None:
    await run_ask(env, "Disk usage jumped 13% overnight, mostly in /mnt/data/downloads")
    (sent,) = env.telegram.sent()
    assert sent.text == (
        "[disk-check] Disk usage jumped 13% overnight, mostly in /mnt/data/downloads"
    )


# --- Target user (change add-multiple-users) ------------------------------------------


async def test_notify_a_named_user(env: Env) -> None:
    response = await env.client.post(
        "/notify", json={"text": "Dune finished downloading", "user": "ana"}, headers=BACKUP
    )
    assert response.status_code == 200
    (sent,) = env.telegram.sent()
    assert sent.chat_id == ANA_ID and sent.text == "[backup] Dune finished downloading"
    assert [e.text for e in await env.history.recent("ana", 10)] == ["Dune finished downloading"]
    assert await env.history.recent("diogo", 10) == []


async def test_notify_without_a_user_goes_to_the_admin(env: Env) -> None:
    await env.client.post("/notify", json={"text": "backup done"}, headers=BACKUP)
    (sent,) = env.telegram.sent()
    assert sent.chat_id == DIOGO.telegram_user_id
    assert [e.text for e in await env.history.recent("diogo", 10)] == ["backup done"]


@pytest.mark.parametrize("path", ["/notify", "/ask"])
async def test_unknown_user_is_rejected(env: Env, path: str) -> None:
    body = {"text": "x", "prompt": "x", "user": "anna"}
    response = await env.client.post(path, json=body, headers=BACKUP)
    assert response.status_code == 422
    assert "unknown user 'anna'" in response.text
    assert env.telegram.requests == []
    assert await env.items() == []
    assert await env.history.recent("diogo", 10) == []


async def test_ask_for_a_named_user(env: Env) -> None:
    response = await env.client.post(
        "/ask", json={"prompt": "Dune finished", "user": "ana"}, headers=DISK
    )
    assert response.status_code == 202
    (item,) = await env.items()
    assert (item.kind, item.user_name, item.source) == ("ask", "ana", "disk-check")


async def test_ask_without_a_user_is_for_the_admin(env: Env) -> None:
    await env.client.post("/ask", json={"prompt": "93%"}, headers=DISK)
    (item,) = await env.items()
    assert item.user_name == "diogo"
