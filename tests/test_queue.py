from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.model import ModelFailed, ModelUnreachable
from alfred.agent.prompts import NO_REPLY
from alfred.agent.turn import END_CONVERSATION, Agent
from alfred.config import QueueConfig
from alfred.db.models import QueueItem
from alfred.history import History
from alfred.queue import NEW_CONVERSATION_MARKER, Queue, Worker, backoff_s
from tests.helpers import FakeModel, calls, make_config, text
from tests.mcp.servers import config as server_config
from tests.mcp.servers import in_process, make_mcparr

FAST = {"retry_initial_s": 0.01, "retry_max_s": 0.04}


class FakeOutbox:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text_: str) -> None:
        self.sent.append(text_)


class Setup:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], model: FakeModel) -> None:
        cfg = make_config(queue=FAST, mcp_servers=[server_config("mcparr")])
        self.sessions = sessions
        self.model = model
        self.history = History(sessions)
        self.queue = Queue(sessions)
        self.outbox = FakeOutbox()
        connect = in_process({"mcparr": make_mcparr()})
        self.agent = Agent(cfg, model, self.history, sessions, connect)
        self.worker = Worker(self.queue, self.agent, self.history, self.outbox, cfg.queue)

    async def drain(self) -> None:
        while await self.worker.step():
            pass

    async def items(self) -> list[QueueItem]:
        async with self.sessions() as session:
            return list(await session.scalars(select(QueueItem).order_by(QueueItem.id)))


def user_messages(model: FakeModel, request: int) -> list[str]:
    return [m["content"] for m in model.requests[request][0] if m["role"] != "system"]


# --- Single ordered worker -----------------------------------------------------


async def test_two_messages_in_quick_succession(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    s = Setup(sessions, FakeModel([text("Added Dune."), text("Nothing.")]))
    await s.queue.enqueue_owner("add Dune Part Three", 1)
    await s.queue.enqueue_owner("and what's downloading?", 2)
    await s.drain()
    assert s.outbox.sent == ["Added Dune.", "Nothing."]
    # The second turn sees the first turn in its context.
    assert user_messages(s.model, 1) == [
        "add Dune Part Three",
        "Added Dune.",
        "and what's downloading?",
    ]
    assert [i.status for i in await s.items()] == ["done", "done"]


async def test_owner_message_and_ask_interleaved(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    s = Setup(sessions, FakeModel([text("owner reply"), text("disk alert")]))
    await s.queue.enqueue_owner("hi")
    await s.queue.enqueue_ask("disk-check", "93%")
    await s.drain()
    assert s.outbox.sent == ["owner reply", "[disk-check] disk alert"]


async def test_one_turn_at_a_time(sessions: async_sessionmaker[AsyncSession]) -> None:
    running = 0
    peak = 0

    class SlowAgent:
        async def run(self, turn):  # type: ignore[no-untyped-def]
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1
            from alfred.agent.turn import TurnResult

            return TurnResult("ok", False, False)

    s = Setup(sessions, FakeModel())
    worker = Worker(s.queue, SlowAgent(), s.history, s.outbox, QueueConfig(**FAST))
    task = asyncio.create_task(worker.run_forever())
    for i in range(3):
        await s.queue.enqueue_owner(f"m{i}")
    for _ in range(100):
        if len(s.outbox.sent) == 3:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    assert s.outbox.sent == ["ok", "ok", "ok"]
    assert peak == 1


# --- Queue survives restarts ---------------------------------------------------


async def test_restart_with_pending_items(sessions: async_sessionmaker[AsyncSession]) -> None:
    before = Setup(sessions, FakeModel())
    await before.queue.enqueue_owner("first")
    await before.queue.enqueue_owner("second")
    # A new worker (a restarted process) picks them up in order.
    after = Setup(sessions, FakeModel([text("r1"), text("r2")]))
    await after.drain()
    assert after.outbox.sent == ["r1", "r2"]
    assert user_messages(after.model, 0)[-1] == "first"
    assert user_messages(after.model, 1)[-1] == "second"


# --- Wait for an unreachable AI ------------------------------------------------


def test_backoff_doubles_up_to_max() -> None:
    cfg = QueueConfig(retry_initial_s=30, retry_max_s=900)
    assert [backoff_s(n, cfg) for n in range(1, 8)] == [30, 60, 120, 240, 480, 900, 900]


async def test_ai_server_down(sessions: async_sessionmaker[AsyncSession]) -> None:
    down = ModelUnreachable("refused")
    s = Setup(sessions, FakeModel([down, down, down, text("Here you go.")]))
    await s.queue.enqueue_owner("what's downloading?")

    assert await s.worker.step()  # attempt 1 fails
    (item,) = await s.items()
    assert item.status == "pending" and item.attempts == 1
    first_retry_at = item.next_attempt_at

    await s.drain()
    (item,) = await s.items()
    assert item.status == "done" and item.attempts == 3
    assert item.next_attempt_at > first_retry_at
    assert s.outbox.sent == ["Here you go."]
    # The owner message is recorded once, however many attempts it took.
    assert [e.text for e in await s.history.recent(30)] == ["what's downloading?", "Here you go."]


async def test_head_of_line_waits_for_retry(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(sessions, FakeModel([ModelUnreachable("x"), text("first"), text("second")]))
    s.worker = Worker(
        s.queue, s.agent, s.history, s.outbox, QueueConfig(retry_initial_s=0.3, retry_max_s=1)
    )
    await s.queue.enqueue_owner("one")
    await s.worker.step()  # fails, retry in 0.3 s
    await s.queue.enqueue_owner("two")
    # The next step waits for the head's retry time instead of taking "two".
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(s.worker.step(), 0.1)
    assert len(s.model.requests) == 1
    await s.drain()
    assert s.outbox.sent == ["first", "second"]


async def test_order_preserved_during_outage(sessions: async_sessionmaker[AsyncSession]) -> None:
    down = ModelUnreachable("refused")
    s = Setup(sessions, FakeModel([down, down, text("a"), text("b"), text("c")]))
    for m in ("one", "two", "three"):
        await s.queue.enqueue_owner(m)
    await s.drain()
    assert s.outbox.sent == ["a", "b", "c"]
    assert [user_messages(s.model, i)[-1] for i in (2, 3, 4)] == ["one", "two", "three"]


# --- No repeat after tools ran -------------------------------------------------


async def test_ai_drops_mid_turn(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(
        sessions,
        FakeModel(
            [
                calls(("mcparr_add_media", '{"title": "Dune"}')),
                ModelUnreachable("timeout"),
                text("x"),
            ]
        ),
    )
    await s.queue.enqueue_owner("add Dune")
    await s.queue.enqueue_owner("next")
    await s.drain()
    first, second = await s.items()
    assert first.status == "failed" and first.attempts == 0
    assert "not retried" in (first.failure_reason or "")
    assert "partly completed" in s.outbox.sent[0]
    assert second.status == "done"


# --- Non-retryable failures ----------------------------------------------------


async def test_model_rejects_request(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(sessions, FakeModel([ModelFailed("model server answered HTTP 400: bad"), text("ok")]))
    await s.queue.enqueue_owner("hi")
    await s.queue.enqueue_owner("again")
    await s.drain()
    first, second = await s.items()
    assert first.status == "failed" and "HTTP 400" in (first.failure_reason or "")
    assert first.finished_at is not None
    assert s.outbox.sent == ["Sorry, that request failed.", "ok"]
    assert second.status == "done"


async def test_failed_ask_sends_nothing(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(sessions, FakeModel([ModelFailed("HTTP 400")]))
    await s.queue.enqueue_ask("disk-check", "x")
    await s.drain()
    assert s.outbox.sent == []
    assert (await s.items())[0].status == "failed"


async def test_unexpected_error_fails_item(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(sessions, FakeModel([RuntimeError("boom secret-ish detail")]))
    await s.queue.enqueue_owner("hi")
    await s.drain()
    (item,) = await s.items()
    assert item.status == "failed"
    assert s.outbox.sent == ["Sorry, that request failed."]


# --- Ask turns in the conversation --------------------------------------------


async def test_silent_ask_not_recorded(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    s = Setup(sessions, FakeModel([text(f"  {NO_REPLY}\n")]))
    item_id = await s.queue.enqueue_ask("disk-check", "all fine")
    with caplog.at_level("INFO"):
        await s.drain()
    assert s.outbox.sent == []
    assert await s.history.recent(30) == []
    assert "disk-check" in caplog.text and str(item_id) in caplog.text and NO_REPLY in caplog.text
    assert (await s.items())[0].status == "done"


async def test_notifying_ask_recorded(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(sessions, FakeModel([text("Disk jumped 13%."), text("It's downloads.")]))
    await s.queue.enqueue_ask("disk-check", "/mnt/data at 93%")
    await s.queue.enqueue_owner("what's using it?")
    await s.drain()
    assert s.outbox.sent[0] == "[disk-check] Disk jumped 13%."
    assert user_messages(s.model, 1) == [
        "[ask:disk-check] /mnt/data at 93%",
        "Disk jumped 13%.",
        "what's using it?",
    ]


# --- Reset after the turn ------------------------------------------------------


async def test_reset_sequence(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(
        sessions,
        FakeModel([calls((END_CONVERSATION, "{}")), text("Done 👍"), text("Hi again")]),
    )
    await s.queue.enqueue_owner("great, that's sorted, we're done here")
    await s.drain()
    assert s.outbox.sent == ["Done 👍", NEW_CONVERSATION_MARKER]

    await s.queue.enqueue_owner("new question")
    await s.drain()
    assert user_messages(s.model, 2) == ["new question"]


async def test_notice_after_reset_in_new_conversation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    s = Setup(sessions, FakeModel([calls((END_CONVERSATION, "{}")), text("Bye")]))
    await s.queue.enqueue_owner("we're done")
    await s.drain()
    await s.history.append("notice", "backup ok", source="backup")
    assert [e.text for e in await s.history.recent(30)] == ["backup ok"]
