from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy import text as sql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred import notices
from alfred.agent.brains import Brains
from alfred.agent.model import ModelFailed, ModelReply, ModelUnreachable
from alfred.agent.prompts import NO_REPLY
from alfred.agent.turn import END_CONVERSATION, Agent
from alfred.config import QueueConfig
from alfred.db.models import QueueItem
from alfred.history import History
from alfred.memory.service import Memory
from alfred.queue import Queue, Worker, backoff_s, reset_marker
from tests.helpers import FakeEmbedder, FakeModel, calls, make_config, text
from tests.mcp.servers import config as server_config
from tests.mcp.servers import in_process, make_mcparr

FAST = {"retry_initial_s": 0.01, "retry_max_s": 0.04}


class FakeOutbox:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text_: str) -> None:
        self.sent.append(text_)


class Setup:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        model: FakeModel,
        summary_model: object | None = None,
        brains: Brains | None = None,
    ) -> None:
        cfg = make_config(queue=FAST, mcp_servers=[server_config("mcparr")])
        self.sessions = sessions
        self.model = model
        self.history = History(sessions)
        self.queue = Queue(sessions)
        self.outbox = FakeOutbox()
        connect = in_process({"mcparr": make_mcparr()})
        self.agent = Agent(cfg, model, self.history, sessions, connect, brains=brains)
        self.memory = None
        if summary_model is not None:
            self.memory = Memory(
                cfg,
                sessions,
                summary_model,
                FakeEmbedder(default=[1.0]),  # type: ignore[arg-type]
            )
        self.worker = Worker(
            self.queue,
            self.agent,
            self.history,
            self.outbox,
            cfg.queue,
            owner_address=cfg.ai.owner_address,
            memory=self.memory,
        )

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
    worker = Worker(
        s.queue, SlowAgent(), s.history, s.outbox, QueueConfig(**FAST), owner_address="sir"
    )
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
        s.queue,
        s.agent,
        s.history,
        s.outbox,
        QueueConfig(retry_initial_s=0.3, retry_max_s=1),
        owner_address="sir",
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
    assert s.outbox.sent[0] == notices.unreachable_mid("sir")
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
    assert s.outbox.sent == [notices.failed("sir"), "ok"]
    assert second.status == "done"


async def test_failed_ask_sends_nothing(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(sessions, FakeModel([ModelFailed("HTTP 400")]))
    await s.queue.enqueue_ask("disk-check", "x")
    await s.drain()
    assert s.outbox.sent == []
    assert (await s.items())[0].status == "failed"


async def test_unexpected_error_fails_item(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(sessions, FakeModel([RuntimeError("boom secret-ish detail")]))
    s.worker = Worker(
        s.queue, s.agent, s.history, s.outbox, QueueConfig(**FAST), owner_address="Mr. Ferreira"
    )
    await s.queue.enqueue_owner("hi")
    await s.drain()
    (item,) = await s.items()
    assert item.status == "failed"
    assert s.outbox.sent == [notices.failed("Mr. Ferreira")]


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


SUMMARY = "Title: Chicken thighs recipe\nSummary: Lemon and garlic, 40 minutes at 200°C."


async def start_at_41(sessions: async_sessionmaker[AsyncSession]) -> None:
    """Make the current conversation #41, as in the spec's examples."""
    async with sessions.begin() as session:
        await session.execute(sql("INSERT INTO conversations (id) VALUES (41)"))
        await session.execute(sql("SELECT setval('conversations_id_seq', 41)"))


def test_reset_marker() -> None:
    assert reset_marker(41, "Chicken thighs recipe", 42) == (
        '— #41 "Chicken thighs recipe" —\n— new conversation #42 —'
    )
    assert reset_marker(41, None, 42) == "— #41 archived —\n— new conversation #42 —"
    assert reset_marker(None, None, 1) == "— new conversation #1 —"


async def test_reset_sequence(sessions: async_sessionmaker[AsyncSession]) -> None:
    await start_at_41(sessions)
    s = Setup(
        sessions,
        FakeModel([calls((END_CONVERSATION, "{}")), text("Done 👍"), text("Hi again")]),
        summary_model=FakeModel([text(SUMMARY)]),
    )
    await s.queue.enqueue_owner("great, that's sorted, we're done here")
    await s.drain()
    assert s.outbox.sent == [
        "Done 👍",
        '— #41 "Chicken thighs recipe" —\n— new conversation #42 —',
    ]

    await s.queue.enqueue_owner("new question")
    await s.drain()
    assert user_messages(s.model, 2) == ["new question"]


async def test_reset_marker_without_title_when_summary_fails(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await start_at_41(sessions)
    s = Setup(
        sessions,
        FakeModel([calls((END_CONVERSATION, "{}")), text("Done 👍")]),
        summary_model=FakeModel([ModelUnreachable("brainiac asleep")]),
    )
    await s.queue.enqueue_owner("we're done")
    await s.drain()
    assert s.outbox.sent == ["Done 👍", "— #41 archived —\n— new conversation #42 —"]
    assert [i.status for i in await s.items()] == ["done"]


async def test_notice_during_summary_lands_in_new_conversation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await start_at_41(sessions)
    history = History(sessions)

    transcripts: list[str] = []

    class NoticeWhileSummarising:
        async def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            await history.append("notice", "backup ok", source="backup")
            transcripts.append(messages[-1]["content"])
            return ModelReply(content=SUMMARY)

    s = Setup(
        sessions,
        FakeModel([calls((END_CONVERSATION, "{}")), text("Bye")]),
        summary_model=NoticeWhileSummarising(),
    )
    await s.queue.enqueue_owner("we're done")
    await s.drain()
    assert await history.current_conversation_id() == 42
    assert [e.text for e in await history.recent(30)] == ["backup ok"]
    assert s.outbox.sent[-1].startswith('— #41 "Chicken thighs recipe" —')
    # The summary is of the archived conversation only.
    assert transcripts == ["Owner: we're done\nalfred: Bye"]


async def test_reset_without_memory_sends_ids(sessions: async_sessionmaker[AsyncSession]) -> None:
    await start_at_41(sessions)
    s = Setup(sessions, FakeModel([calls((END_CONVERSATION, "{}")), text("Bye")]))
    await s.queue.enqueue_owner("we're done")
    await s.drain()
    assert s.outbox.sent == ["Bye", "— #41 archived —\n— new conversation #42 —"]


async def test_notice_after_reset_in_new_conversation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    s = Setup(sessions, FakeModel([calls((END_CONVERSATION, "{}")), text("Bye")]))
    await s.queue.enqueue_owner("we're done")
    await s.drain()
    await s.history.append("notice", "backup ok", source="backup")
    assert [e.text for e in await s.history.recent(30)] == ["backup ok"]


# --- Typing indicator ----------------------------------------------------------


class RecordingTyping:
    """Records indicator enters and exits, interleaved with outbox sends."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    @contextlib.asynccontextmanager
    async def __call__(self) -> AsyncIterator[None]:
        self.events.append("typing on")
        try:
            yield
        finally:
            self.events.append("typing off")


class EventOutbox:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def send(self, text_: str) -> None:
        self.events.append(f"send {text_}")


def typing_worker(s: Setup, events: list[str], queue: QueueConfig | None = None) -> Worker:
    return Worker(
        s.queue,
        s.agent,
        s.history,
        EventOutbox(events),
        queue or QueueConfig(**FAST),
        owner_address="sir",
        typing=RecordingTyping(events),
    )


async def test_typing_while_owner_turn_runs(sessions: async_sessionmaker[AsyncSession]) -> None:
    events: list[str] = []

    class Agent_:
        async def run(self, turn):  # type: ignore[no-untyped-def]
            events.append("turn")
            from alfred.agent.turn import TurnResult

            return TurnResult("Added Dune.", False, False)

    s = Setup(sessions, FakeModel())
    s.agent = Agent_()  # type: ignore[assignment]
    worker = typing_worker(s, events)
    await s.queue.enqueue_owner("add Dune")
    while await worker.step():
        pass
    assert events == ["typing on", "turn", "typing off", "send Added Dune."]


async def test_no_typing_for_ask(sessions: async_sessionmaker[AsyncSession]) -> None:
    events: list[str] = []
    s = Setup(sessions, FakeModel([text("disk alert")]))
    worker = typing_worker(s, events)
    await s.queue.enqueue_ask("disk-check", "93%")
    while await worker.step():
        pass
    assert events == ["send [disk-check] disk alert"]


async def test_typing_stops_during_retry_wait(sessions: async_sessionmaker[AsyncSession]) -> None:
    events: list[str] = []
    s = Setup(sessions, FakeModel([ModelUnreachable("refused"), text("Here you go.")]))
    # Long enough that the second step is certainly the backoff wait.
    worker = typing_worker(s, events, QueueConfig(retry_initial_s=0.2, retry_max_s=1))
    await s.queue.enqueue_owner("what's downloading?")
    assert await worker.step()  # attempt 1 fails and is scheduled for a retry
    assert events == ["typing on", "typing off"]
    assert await worker.step()  # the backoff wait: no turn runs, no typing
    assert events == ["typing on", "typing off"]
    while await worker.step():
        pass
    assert events == ["typing on", "typing off", "typing on", "typing off", "send Here you go."]


async def test_typing_stops_before_failure_notice(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    events: list[str] = []
    s = Setup(sessions, FakeModel([ModelFailed("model server answered HTTP 400")]))
    worker = typing_worker(s, events)
    await s.queue.enqueue_owner("status?")
    while await worker.step():
        pass
    assert events == ["typing on", "typing off", f"send {notices.failed('sir')}"]


# --- Brain fallback notice -------------------------------------------------------

BAD_MODEL = ModelFailed("model server answered HTTP 400: model does not support tools")
FELL_BACK = notices.fell_back("sir", "bonsai", "gemma")


def on_bonsai() -> Brains:
    brains = Brains("gemma4:26b-a4b-it-qat", {"gemma": "gemma4:26b-a4b-it-qat", "bonsai": "b:8b"})
    brains.set("bonsai")
    return brains


async def test_fallback_notice_before_the_reply(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    s = Setup(sessions, FakeModel([BAD_MODEL, text("gemma's answer")]), brains=on_bonsai())
    await s.queue.enqueue_owner("hi")
    await s.drain()
    assert s.outbox.sent == [FELL_BACK, "gemma's answer"]
    contents = [e.text for e in await s.history.recent(10)]
    assert contents == ["hi", "gemma's answer"]


async def test_fallback_notice_before_the_failure_notice(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([calls(("mcparr_get_downloads", "{}")), BAD_MODEL])
    s = Setup(sessions, model, brains=on_bonsai())
    await s.queue.enqueue_owner("downloads?")
    await s.drain()
    assert s.outbox.sent == [FELL_BACK, notices.failed("sir", True)]


async def test_fallback_notice_on_a_no_reply_ask(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    s = Setup(sessions, FakeModel([BAD_MODEL, text(NO_REPLY)]), brains=on_bonsai())
    await s.queue.enqueue_ask("disk-check", "93%")
    await s.drain()
    assert s.outbox.sent == [FELL_BACK]


async def test_fallback_notice_on_a_failed_ask(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    s = Setup(sessions, FakeModel([BAD_MODEL, BAD_MODEL]), brains=on_bonsai())
    await s.queue.enqueue_ask("disk-check", "93%")
    await s.drain()
    assert s.outbox.sent == [FELL_BACK]


async def test_fallback_notice_sent_once_across_a_retry(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([BAD_MODEL, ModelUnreachable("model request timed out"), text("ok")])
    s = Setup(sessions, model, brains=on_bonsai())
    await s.queue.enqueue_owner("hi")
    await s.drain()
    assert s.outbox.sent == [FELL_BACK, "ok"]


async def test_summary_ignores_the_chosen_brain(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    # One client for turns and summaries, as in serve when no summary model is
    # set: turns ask for the brain's tag, the summary for the client's own model.
    await start_at_41(sessions)
    model = FakeModel([calls((END_CONVERSATION, "{}")), text("Done."), text(SUMMARY)])
    s = Setup(sessions, model, summary_model=model, brains=on_bonsai())
    await s.queue.enqueue_owner("we're done")
    await s.drain()
    assert model.models == ["b:8b", "b:8b", None]
