from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy import text as sql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred import notices
from alfred.agent.messages import strip_stamp
from alfred.agent.prompts import _NO_REPLY
from alfred.attachments import NewAttachment
from alfred.backoff import backoff_s
from alfred.brains import Brains
from alfred.config import QueueConfig
from alfred.db.models import Attachment, Entry, QueueItem
from alfred.history import History
from alfred.inputs.parking import Parking
from alfred.kernel.access import AllowList
from alfred.kernel.users import User, Users
from alfred.llm.chat import ModelFailed, ModelUnreachable
from alfred.memory.summaries import Summaries
from alfred.notices import reset_marker
from alfred.queue import Queue
from alfred.reminders.store import Reminders
from alfred.reminders.tools import _REMIND_SET, ReminderTools
from alfred.tools.conversation import END_CONVERSATION
from alfred.worker import Worker
from tests.fakes import NoInputRequests, NoQuestions, NoReminders, ignore, no_typing
from tests.helpers import (
    ANA,
    DIOGO,
    FakeEmbedder,
    FakeModel,
    calls,
    make_agent,
    make_config,
    text,
)
from tests.mcp.servers import config as server_config
from tests.mcp.servers import in_process, make_mcparr
from tests.test_queue import Ticks

FAST = {"retry_initial_s": 0.01, "retry_max_s": 0.04}
LISBON = ZoneInfo("Europe/Lisbon")


class FakeOutbox:
    def __init__(self) -> None:
        self.sent: list[str] = []
        # (user name, text), in order.
        self.sent_to: list[tuple[str, str]] = []

    async def send(self, user: User, text_: str) -> None:
        self.sent.append(text_)
        self.sent_to.append((user.name, text_))


class Setup:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        model: FakeModel,
        summary_model: object | None = None,
        brains: Brains | None = None,
        users: tuple[User, ...] = (DIOGO,),
        clock: Callable[[], float] | None = None,
    ) -> None:
        cfg = make_config(queue=FAST, mcp_servers=[server_config("mcparr")], users=users)
        self.sessions = sessions
        self.reminders = Reminders(sessions)
        tools = ReminderTools(self.reminders, LISBON, "09:00", cfg.users)
        self.model = model
        self.history = History(sessions)
        self.queue = Queue(sessions) if clock is None else Queue(sessions, clock=clock)
        self.outbox = FakeOutbox()
        connect = in_process({"mcparr": make_mcparr()})
        self.agent = make_agent(
            cfg, model, self.history, sessions, connect, reminders=tools, brains=brains
        )
        self.memory = None
        if summary_model is not None:
            self.memory = Summaries(
                cfg,
                sessions,
                summary_model,
                FakeEmbedder(default=[1.0]),  # type: ignore[arg-type]
            )
        # Times the worker woke the memory sweeper.
        self.nudges = 0
        self.worker = Worker(
            self.queue,
            self.agent,
            self.history,
            self.outbox,
            cfg.queue,
            users=cfg.users,
            nudge=self._nudge,
            reminders=self.reminders,
            zone=LISBON,
            parking=Parking(NoInputRequests(), NoQuestions()),
            typing=no_typing,
        )

    def _nudge(self) -> None:
        self.nudges += 1

    async def drain(self) -> None:
        while await self.worker._step():
            pass

    async def items(self) -> list[QueueItem]:
        async with self.sessions() as session:
            return list(await session.scalars(select(QueueItem).order_by(QueueItem.id)))


def user_messages(model: FakeModel, request: int) -> list[str]:
    """The non-system messages' text, without their times (the database's clock)."""
    return [strip_stamp(m["content"]) for m in model.requests[request][0] if m["role"] != "system"]


# --- Single ordered worker -----------------------------------------------------


async def test_two_messages_in_quick_succession(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    s = Setup(sessions, FakeModel([text("Added Dune."), text("Nothing.")]))
    await s.queue.enqueue_user("diogo", "add Dune Part Three", 1)
    await s.queue.enqueue_user("diogo", "and what's downloading?", 2)
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
    await s.queue.enqueue_user("diogo", "hi")
    await s.queue.enqueue_ask("disk-check", "93%", "diogo")
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
        s.queue,
        SlowAgent(),
        s.history,
        s.outbox,
        QueueConfig(**FAST),
        users=Users((DIOGO,)),
        parking=Parking(NoInputRequests(), NoQuestions()),
        reminders=NoReminders(),
        typing=no_typing,
        nudge=ignore,
    )
    task = asyncio.create_task(worker.run_forever())
    for i in range(3):
        await s.queue.enqueue_user("diogo", f"m{i}")
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
    await before.queue.enqueue_user("diogo", "first")
    await before.queue.enqueue_user("diogo", "second")
    # A new worker (a restarted process) picks them up in order.
    after = Setup(sessions, FakeModel([text("r1"), text("r2")]))
    await after.drain()
    assert after.outbox.sent == ["r1", "r2"]
    assert user_messages(after.model, 0)[-1] == "first"
    assert user_messages(after.model, 1)[-1] == "second"


# --- Wait for an unreachable AI ------------------------------------------------


def test_backoff_doubles_up_to_max() -> None:
    cfg = QueueConfig(retry_initial_s=30, retry_max_s=900)
    assert [backoff_s(n, cfg.retry_initial_s, cfg.retry_max_s) for n in range(1, 8)] == [
        30,
        60,
        120,
        240,
        480,
        900,
        900,
    ]


async def test_ai_server_down(sessions: async_sessionmaker[AsyncSession]) -> None:
    down = ModelUnreachable("refused")
    s = Setup(sessions, FakeModel([down, down, down, text("Here you go.")]))
    await s.queue.enqueue_user("diogo", "what's downloading?")

    assert await s.worker._step()  # attempt 1 fails
    (item,) = await s.items()
    assert item.status == "pending" and item.attempts == 1
    first_retry_at = item.next_attempt_at

    await s.drain()
    (item,) = await s.items()
    assert item.status == "done" and item.attempts == 3
    assert item.next_attempt_at > first_retry_at
    assert s.outbox.sent == ["Here you go."]
    # The owner message is recorded once, however many attempts it took.
    assert [e.text for e in await s.history.recent("diogo", 30)] == [
        "what's downloading?",
        "Here you go.",
    ]


async def test_head_of_line_waits_for_retry(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(sessions, FakeModel([ModelUnreachable("x"), text("first"), text("second")]))
    s.worker = Worker(
        s.queue,
        s.agent,
        s.history,
        s.outbox,
        QueueConfig(retry_initial_s=0.3, retry_max_s=1),
        users=Users((DIOGO,)),
        parking=Parking(NoInputRequests(), NoQuestions()),
        reminders=NoReminders(),
        typing=no_typing,
        nudge=ignore,
    )
    await s.queue.enqueue_user("diogo", "one")
    await s.worker._step()  # fails, retry in 0.3 s
    await s.queue.enqueue_user("diogo", "two")
    # The next step waits for the head's retry time instead of taking "two".
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(s.worker._step(), 0.1)
    assert len(s.model.requests) == 1
    await s.drain()
    assert s.outbox.sent == ["first", "second"]


async def test_order_preserved_during_outage(sessions: async_sessionmaker[AsyncSession]) -> None:
    down = ModelUnreachable("refused")
    s = Setup(sessions, FakeModel([down, down, text("a"), text("b"), text("c")]))
    for m in ("one", "two", "three"):
        await s.queue.enqueue_user("diogo", m)
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
    await s.queue.enqueue_user("diogo", "add Dune")
    await s.queue.enqueue_user("diogo", "next")
    await s.drain()
    first, second = await s.items()
    assert first.status == "failed" and first.attempts == 0
    assert "not retried" in (first.failure_reason or "")
    assert s.outbox.sent[0] == notices.unreachable_mid("sir")
    assert second.status == "done"


# --- Non-retryable failures ----------------------------------------------------


async def test_model_rejects_request(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(sessions, FakeModel([ModelFailed("model server answered HTTP 400: bad"), text("ok")]))
    await s.queue.enqueue_user("diogo", "hi")
    await s.queue.enqueue_user("diogo", "again")
    await s.drain()
    first, second = await s.items()
    assert first.status == "failed" and "HTTP 400" in (first.failure_reason or "")
    assert first.finished_at is not None
    assert s.outbox.sent == [notices.failed("sir"), "ok"]
    assert second.status == "done"


async def test_failed_ask_sends_nothing(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(sessions, FakeModel([ModelFailed("HTTP 400")]))
    await s.queue.enqueue_ask("disk-check", "x", "diogo")
    await s.drain()
    assert s.outbox.sent == []
    assert (await s.items())[0].status == "failed"


async def test_unexpected_error_fails_item(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(sessions, FakeModel([RuntimeError("boom secret-ish detail")]))
    s.worker = Worker(
        s.queue,
        s.agent,
        s.history,
        s.outbox,
        QueueConfig(**FAST),
        users=Users((dataclasses.replace(DIOGO, address="Mr. Ferreira"),)),
        parking=Parking(NoInputRequests(), NoQuestions()),
        reminders=NoReminders(),
        typing=no_typing,
        nudge=ignore,
    )
    await s.queue.enqueue_user("diogo", "hi")
    await s.drain()
    (item,) = await s.items()
    assert item.status == "failed"
    assert s.outbox.sent == [notices.failed("Mr. Ferreira")]


# --- Ask turns in the conversation --------------------------------------------


async def test_silent_ask_not_recorded(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    s = Setup(sessions, FakeModel([text(f"  {_NO_REPLY}\n")]))
    item_id = await s.queue.enqueue_ask("disk-check", "all fine", "diogo")
    with caplog.at_level("INFO"):
        await s.drain()
    assert s.outbox.sent == []
    assert await s.history.recent("diogo", 30) == []
    assert "disk-check" in caplog.text and str(item_id) in caplog.text and _NO_REPLY in caplog.text
    assert (await s.items())[0].status == "done"


async def test_notifying_ask_recorded(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(sessions, FakeModel([text("Disk jumped 13%."), text("It's downloads.")]))
    await s.queue.enqueue_ask("disk-check", "/mnt/data at 93%", "diogo")
    await s.queue.enqueue_user("diogo", "what's using it?")
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
        await session.execute(sql("INSERT INTO conversations (id, user_name) VALUES (41, 'diogo')"))
        await session.execute(sql("SELECT setval('conversations_id_seq', 41)"))


def testreset_marker() -> None:
    assert reset_marker(41, 42) == "— #41 archived —\n— new conversation #42 —"
    assert reset_marker(41, 42, idle=True) == (
        "— #41 closed after inactivity —\n— new conversation #42 —"
    )
    assert reset_marker(None, 1) == "— new conversation #1 —"
    assert reset_marker(None, 1, idle=True) == "— new conversation #1 —"


class BlockingModel:
    """A summary model that never answers; records that it was asked."""

    def __init__(self) -> None:
        self.asked = asyncio.Event()

    async def complete_streamed(self, messages, *, model=None):  # type: ignore[no-untyped-def]
        self.asked.set()
        await asyncio.Event().wait()


async def test_reset_sequence(sessions: async_sessionmaker[AsyncSession]) -> None:
    await start_at_41(sessions)
    summary_model = BlockingModel()
    s = Setup(
        sessions,
        FakeModel([calls((END_CONVERSATION, "{}")), text("Done 👍"), text("Hi again")]),
        summary_model=summary_model,
    )
    await s.queue.enqueue_user("diogo", "great, that's sorted, we're done here")
    await s.drain()
    assert s.outbox.sent == ["Done 👍", "— #41 archived —\n— new conversation #42 —"]
    assert s.nudges == 1
    assert not summary_model.asked.is_set()  # the worker never summarises

    await s.queue.enqueue_user("diogo", "new question")
    await s.drain()
    assert user_messages(s.model, 2) == ["new question"]


async def test_next_message_does_not_wait_for_the_summary(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await start_at_41(sessions)
    summary_model = BlockingModel()
    s = Setup(
        sessions,
        FakeModel([calls((END_CONVERSATION, "{}")), text("Done 👍"), text("Dinner: soup")]),
        summary_model=summary_model,
    )
    assert s.memory is not None
    await s.queue.enqueue_user("diogo", "we're done")
    await s.queue.enqueue_user("diogo", "new topic: what's for dinner?")
    await s.worker._step()
    # #41's summary is being made (and never finishes) while the next item runs.
    summarising = asyncio.create_task(s.memory.summarise(41))
    try:
        await asyncio.wait_for(summary_model.asked.wait(), 5)
        await asyncio.wait_for(s.drain(), 5)
    finally:
        summarising.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await summarising
    assert s.outbox.sent[-1] == "Dinner: soup"
    assert [i.status for i in await s.items()] == ["done", "done"]


async def test_reset_end_reason_is_user(sessions: async_sessionmaker[AsyncSession]) -> None:
    await start_at_41(sessions)
    s = Setup(sessions, FakeModel([calls((END_CONVERSATION, "{}")), text("Bye")]))
    await s.queue.enqueue_user("diogo", "we're done")
    await s.drain()
    async with sessions() as session:
        reason = await session.scalar(sql("SELECT end_reason FROM conversations WHERE id = 41"))
    assert reason == "user"


async def test_reset_without_memory_sends_ids(sessions: async_sessionmaker[AsyncSession]) -> None:
    await start_at_41(sessions)
    s = Setup(sessions, FakeModel([calls((END_CONVERSATION, "{}")), text("Bye")]))
    await s.queue.enqueue_user("diogo", "we're done")
    await s.drain()
    assert s.outbox.sent == ["Bye", "— #41 archived —\n— new conversation #42 —"]


async def test_notice_after_reset_in_new_conversation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    s = Setup(sessions, FakeModel([calls((END_CONVERSATION, "{}")), text("Bye")]))
    await s.queue.enqueue_user("diogo", "we're done")
    await s.drain()
    await s.history.append("diogo", "notice", "backup ok", source="backup")
    assert [e.text for e in await s.history.recent("diogo", 30)] == ["backup ok"]


# --- Typing indicator ----------------------------------------------------------


class RecordingTyping:
    """Records indicator enters and exits, interleaved with outbox sends."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    @contextlib.asynccontextmanager
    async def __call__(self, user: User) -> AsyncIterator[None]:
        self.events.append(f"typing on for {user.name}" if user.name != "diogo" else "typing on")
        try:
            yield
        finally:
            self.events.append("typing off")


class EventOutbox:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def send(self, user: User, text_: str) -> None:
        self.events.append(f"send {text_}")


def typing_worker(s: Setup, events: list[str], queue: QueueConfig | None = None) -> Worker:
    return Worker(
        s.queue,
        s.agent,
        s.history,
        EventOutbox(events),
        queue or QueueConfig(**FAST),
        users=Users((DIOGO,)),
        typing=RecordingTyping(events),
        parking=Parking(NoInputRequests(), NoQuestions()),
        reminders=NoReminders(),
        nudge=ignore,
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
    await s.queue.enqueue_user("diogo", "add Dune")
    while await worker._step():
        pass
    assert events == ["typing on", "turn", "typing off", "send Added Dune."]


async def test_no_typing_for_ask(sessions: async_sessionmaker[AsyncSession]) -> None:
    events: list[str] = []
    s = Setup(sessions, FakeModel([text("disk alert")]))
    worker = typing_worker(s, events)
    await s.queue.enqueue_ask("disk-check", "93%", "diogo")
    while await worker._step():
        pass
    assert events == ["send [disk-check] disk alert"]


async def test_typing_stops_during_retry_wait(sessions: async_sessionmaker[AsyncSession]) -> None:
    events: list[str] = []
    s = Setup(sessions, FakeModel([ModelUnreachable("refused"), text("Here you go.")]))
    # Long enough that the second step is certainly the backoff wait.
    worker = typing_worker(s, events, QueueConfig(retry_initial_s=0.2, retry_max_s=1))
    await s.queue.enqueue_user("diogo", "what's downloading?")
    assert await worker._step()  # attempt 1 fails and is scheduled for a retry
    assert events == ["typing on", "typing off"]
    assert await worker._step()  # the backoff wait: no turn runs, no typing
    assert events == ["typing on", "typing off"]
    while await worker._step():
        pass
    assert events == ["typing on", "typing off", "typing on", "typing off", "send Here you go."]


async def test_typing_stops_before_failure_notice(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    events: list[str] = []
    s = Setup(sessions, FakeModel([ModelFailed("model server answered HTTP 400")]))
    worker = typing_worker(s, events)
    await s.queue.enqueue_user("diogo", "status?")
    while await worker._step():
        pass
    assert events == ["typing on", "typing off", f"send {notices.failed('sir')}"]


# --- Brain fallback notice -------------------------------------------------------

BAD_MODEL = ModelFailed("model server answered HTTP 400: model does not support tools")
FELL_BACK = notices.fell_back("sir", "bonsai", "gemma")


def on_bonsai() -> Brains:
    brains = Brains("gemma4:26b-a4b-it-qat", {"gemma": "gemma4:26b-a4b-it-qat", "bonsai": "b:8b"})
    brains.set("bonsai")
    return brains


async def test_fallback_before_the_reply(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    s = Setup(sessions, FakeModel([BAD_MODEL, text("gemma's answer")]), brains=on_bonsai())
    await s.queue.enqueue_user("diogo", "hi")
    await s.drain()
    assert s.outbox.sent == [FELL_BACK, "gemma's answer"]
    contents = [e.text for e in await s.history.recent("diogo", 10)]
    assert contents == ["hi", "gemma's answer"]


async def test_fallback_before_the_failure_notice(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([calls(("mcparr_get_downloads", "{}")), BAD_MODEL])
    s = Setup(sessions, model, brains=on_bonsai())
    await s.queue.enqueue_user("diogo", "downloads?")
    await s.drain()
    assert s.outbox.sent == [FELL_BACK, notices.failed("sir", True)]


async def test_fallback_on_a_no_reply_ask(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    s = Setup(sessions, FakeModel([BAD_MODEL, text(_NO_REPLY)]), brains=on_bonsai())
    await s.queue.enqueue_ask("disk-check", "93%", "diogo")
    await s.drain()
    assert s.outbox.sent == [FELL_BACK]


async def test_fallback_on_a_failed_ask(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    s = Setup(sessions, FakeModel([BAD_MODEL, BAD_MODEL]), brains=on_bonsai())
    await s.queue.enqueue_ask("disk-check", "93%", "diogo")
    await s.drain()
    assert s.outbox.sent == [FELL_BACK]


async def test_fallback_sent_once_across_a_retry(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([BAD_MODEL, ModelUnreachable("model request timed out"), text("ok")])
    s = Setup(sessions, model, brains=on_bonsai())
    await s.queue.enqueue_user("diogo", "hi")
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
    assert s.memory is not None
    await s.queue.enqueue_user("diogo", "we're done")
    await s.drain()
    await s.memory.summarise(41)
    assert model.models == ["b:8b", "b:8b", None]


# --- Reminders -------------------------------------------------------------------


async def due_reminder(s: Setup, text_: str = "call mum", late: timedelta = timedelta()) -> int:
    """A reminder that came due ``late`` ago, claimed into the queue."""
    now = datetime.now(UTC)
    reminder_id = await s.reminders.set(text_, now - late, "diogo", ["diogo"])
    assert len(await s.reminders.claim_due(now)) == 1
    return reminder_id


def reminder_notice(due: datetime, text_: str = "call mum") -> str:
    today = datetime.now(UTC).astimezone(LISBON).date()
    return notices.reminder("sir", text_, due.astimezone(LISBON), today)


async def recorded(s: Setup) -> list[tuple[str, str, int | None]]:
    return [(e.kind, e.text, e.reminder_id) for e in await s.history.recent("diogo", 30)]


async def test_reminder_delivered(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(sessions, FakeModel([text("Your call to mum is due, sir.")]))
    rid = await due_reminder(s)
    await s.drain()
    assert s.outbox.sent == ["Your call to mum is due, sir."]  # no [source] prefix
    assert await recorded(s) == [
        ("reminder", "call mum", rid),
        ("reminder_reply", "Your call to mum is due, sir.", rid),
    ]
    assert [i.status for i in await s.items()] == ["done"]
    last = strip_stamp(s.model.requests[0][0][-1]["content"])
    assert last.startswith(f"[reminder:#{rid} due ") and last.endswith("] call mum")


async def test_reminder_model_error(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(sessions, FakeModel([ModelFailed("model server answered HTTP 400")]))
    rid = await due_reminder(s)
    due = (await s.reminders.get(rid)).due_at  # type: ignore[union-attr]
    await s.drain()
    notice = reminder_notice(due)
    assert s.outbox.sent == [notice]
    assert '"call mum"' in notice and f"{due.astimezone(LISBON):%H:%M}" in notice
    assert await recorded(s) == [("reminder", "call mum", rid), ("reminder_reply", notice, rid)]
    assert [i.status for i in await s.items()] == ["failed"]


@pytest.mark.parametrize("answer", [_NO_REPLY, f"  {_NO_REPLY}\n"])
async def test_reminder_model_says_no_reply(
    sessions: async_sessionmaker[AsyncSession], answer: str
) -> None:
    s = Setup(sessions, FakeModel([text(answer)]))
    rid = await due_reminder(s)
    due = (await s.reminders.get(rid)).due_at  # type: ignore[union-attr]
    await s.drain()
    assert s.outbox.sent == [reminder_notice(due)]
    assert [i.status for i in await s.items()] == ["done"]


async def quiet_reminder(s: Setup, text_: str = "tell me if Dune is done") -> int:
    now = datetime.now(UTC)
    reminder_id = await s.reminders.set(text_, now, "diogo", ["diogo"], quiet_ok=True)
    assert len(await s.reminders.claim_due(now)) == 1
    return reminder_id


@pytest.mark.parametrize("answer", [_NO_REPLY, f"  {_NO_REPLY}\n"])
async def test_quiet_reminder_says_no_reply(
    sessions: async_sessionmaker[AsyncSession], answer: str
) -> None:
    s = Setup(sessions, FakeModel([text(answer)]))
    await quiet_reminder(s)
    await s.drain()
    assert s.outbox.sent == []
    assert await recorded(s) == []
    assert [i.status for i in await s.items()] == ["done"]
    assert _NO_REPLY in s.model.requests[0][0][0]["content"]


async def test_quiet_reminder_fails(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(sessions, FakeModel([ModelFailed("model server answered HTTP 400")]))
    rid = await quiet_reminder(s)
    due = (await s.reminders.get(rid)).due_at  # type: ignore[union-attr]
    await s.drain()
    assert s.outbox.sent == [reminder_notice(due, "tell me if Dune is done")]
    assert [k for k, _, _ in await recorded(s)] == ["reminder", "reminder_reply"]


async def test_quiet_reminder_with_news(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(sessions, FakeModel([text("Dune has finished, sir.")]))
    rid = await quiet_reminder(s)
    await s.drain()
    assert s.outbox.sent == ["Dune has finished, sir."]
    assert await recorded(s) == [
        ("reminder", "tell me if Dune is done", rid),
        ("reminder_reply", "Dune has finished, sir.", rid),
    ]


async def test_repeating_reminder_turn_uses_the_items_due_time(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    from alfred.reminders.when import parse_rule

    s = Setup(sessions, FakeModel([text("Good morning, sir.")]))
    tuesday = datetime(2026, 11, 17, 8, 0, tzinfo=LISBON)
    rule = parse_rule(repeat="weekdays", at="08:00")
    rid = await s.reminders.set("brief me", tuesday, "diogo", ["diogo"], rule=rule)
    await s.reminders.claim_due(tuesday)
    row = await s.reminders.get(rid)
    assert row is not None and row.due_at == datetime(2026, 11, 18, 8, 0, tzinfo=LISBON)
    await s.drain()
    last = strip_stamp(s.model.requests[0][0][-1]["content"])
    assert last.startswith(f"[reminder:#{rid} due Tuesday 17 November 2026, 08:00; now ")
    assert last.endswith("; every weekday at 08:00] brief me")
    assert _NO_REPLY not in s.model.requests[0][0][0]["content"]


async def test_reminder_unexpected_error(sessions: async_sessionmaker[AsyncSession]) -> None:
    class Broken:
        async def run(self, turn):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    s = Setup(sessions, FakeModel())
    s.worker._agent = Broken()  # type: ignore[assignment]
    rid = await due_reminder(s, late=timedelta(days=1))
    due = (await s.reminders.get(rid)).due_at  # type: ignore[union-attr]
    await s.drain()
    notice = reminder_notice(due)
    assert " on " in notice  # due yesterday: the date is shown
    assert s.outbox.sent == [notice]


async def test_reminder_ai_asleep(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([ModelUnreachable("connection refused"), text("Call mum, sir; 0 min late.")])
    s = Setup(sessions, model)
    await due_reminder(s)
    await s.drain()
    # Retried with backoff; no notice in between, the model's answer at the end.
    assert s.outbox.sent == ["Call mum, sir; 0 min late."]
    [item] = await s.items()
    assert (item.status, item.attempts) == ("done", 1)


async def test_reminder_during_an_owner_turn(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(sessions, FakeModel([text("Added Dune."), text("Time to call mum, sir.")]))
    await s.queue.enqueue_user("diogo", "add Dune Part Three")
    rid = await due_reminder(s)
    await s.drain()
    assert s.outbox.sent == ["Added Dune.", "Time to call mum, sir."]
    contents = user_messages(s.model, 1)
    assert contents[:2] == ["add Dune Part Three", "Added Dune."]
    assert contents[2].startswith(f"[reminder:#{rid} due ")


async def test_no_typing_for_reminders(sessions: async_sessionmaker[AsyncSession]) -> None:
    events: list[str] = []
    s = Setup(sessions, FakeModel([text("Call mum, sir.")]))
    worker = typing_worker(s, events)
    worker._reminders = s.reminders
    await due_reminder(s)
    while await worker._step():
        pass
    assert events == ["send Call mum, sir."]


async def test_snooze(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel(
        [
            text("Your call to mum is due, sir."),
            calls((_REMIND_SET, '{"text": "call mum", "in_minutes": 15}')),
            text("Very good. In 15 minutes."),
        ]
    )
    s = Setup(sessions, model)
    rid = await due_reminder(s)
    await s.drain()
    await s.queue.enqueue_user("diogo", "snooze 15 minutes")
    before = datetime.now(UTC)
    await s.drain()
    # The model's context for the snooze has the reminder's text.
    contents = user_messages(model, 1)
    assert contents[0].startswith(f"[reminder:#{rid} due ") and contents[0].endswith("call mum")
    assert contents[1:] == ["Your call to mum is due, sir.", "snooze 15 minutes"]
    [snoozed] = await s.reminders.pending("diogo")
    assert snoozed.text == "call mum" and snoozed.id != rid
    assert timedelta(minutes=14) < snoozed.due_at - before < timedelta(minutes=16)
    assert s.outbox.sent[-1] == "Very good. In 15 minutes."


# --- Attachments (change add-attachments, design D2) -------------------------------


async def test_retried_turn_links_attachments_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    down = ModelUnreachable("refused")
    s = Setup(sessions, FakeModel([down, text("A receipt for 12.40 EUR.")]))
    photo = NewAttachment("image", "image/jpeg", 1000, "attachments/a", width=10, height=10)
    pdf = NewAttachment("pdf", "application/pdf", 2000, "attachments/b", "x.pdf", pages=["p1"])
    item_id = await s.queue.enqueue_user("diogo", "what are these?", 5, [photo, pdf])

    assert await s.worker._step()  # attempt 1: the owner entry is recorded, the AI is down
    await s.drain()

    async with sessions() as session:
        entries = list(await session.scalars(select(Entry).where(Entry.queue_item_id == item_id)))
        attachments = list(await session.scalars(select(Attachment).order_by(Attachment.position)))
    owner = [e for e in entries if e.kind == "user"]
    assert len(owner) == 1
    assert [(a.queue_item_id, a.entry_id) for a in attachments] == [
        (item_id, owner[0].id),
        (item_id, owner[0].id),
    ]
    assert s.outbox.sent == ["A receipt for 12.40 EUR."]


async def test_append_returns_the_entry_id_on_a_retry(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions)
    item = await Queue(sessions).enqueue_user("diogo", "hi")
    first = await history.append("diogo", "user", "hi", queue_item_id=item)
    assert await history.append("diogo", "user", "hi", queue_item_id=item) == first


# --- Users (change add-multiple-users) ---------------------------------------------


async def test_turn_runs_for_the_items_user(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(sessions, FakeModel([text("Lemon and garlic, madam.")]), users=(DIOGO, ANA))
    await s.history.append("diogo", "user", "diogo's secret")
    await s.queue.enqueue_user("ana", "what's for dinner?")
    await s.drain()
    assert s.outbox.sent_to == [("ana", "Lemon and garlic, madam.")]
    assert user_messages(s.model, 0) == ["what's for dinner?"]
    assert [e.text for e in await s.history.recent("ana", 10)] == [
        "what's for dinner?",
        "Lemon and garlic, madam.",
    ]
    assert [e.text for e in await s.history.recent("diogo", 10)] == ["diogo's secret"]


async def test_ask_for_another_user(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(sessions, FakeModel([text("Dune is ready.")]), users=(DIOGO, ANA))
    await s.queue.enqueue_ask("mcparr", "Dune finished", "ana")
    await s.drain()
    assert s.outbox.sent_to == [("ana", "[mcparr] Dune is ready.")]
    assert [e.kind for e in await s.history.recent("ana", 10)] == ["ask_prompt", "ask_reply"]
    assert await s.history.recent("diogo", 10) == []


async def test_item_for_an_unconfigured_user_fails(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    s = Setup(sessions, FakeModel([text("hello")]))
    await s.queue.enqueue_user("bob", "hi")
    await s.queue.enqueue_user("diogo", "hi")
    with caplog.at_level(logging.ERROR):
        await s.drain()
    bob, diogo = await s.items()
    assert (bob.status, bob.failure_reason) == ("failed", "user 'bob' is not configured")
    assert diogo.status == "done"
    assert s.outbox.sent_to == [("diogo", "hello")]
    assert "'bob' is not configured" in caplog.text


async def test_failure_reaches_only_its_user(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(sessions, FakeModel([ModelFailed("HTTP 400")]), users=(DIOGO, ANA))
    await s.queue.enqueue_user("ana", "hi")
    await s.drain()
    assert s.outbox.sent_to == [("ana", notices.failed("madam"))]
    assert s.outbox.sent[0].endswith(", madam.")


async def test_reset_marker_goes_to_its_user(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([calls((END_CONVERSATION, "{}")), text("Bye.")])
    s = Setup(sessions, model, users=(DIOGO, ANA))
    await s.history.append("diogo", "user", "keep this")
    diogo = await s.history.current_conversation_id("diogo")
    await s.queue.enqueue_user("ana", "we're done")
    await s.drain()
    assert [to for to, _ in s.outbox.sent_to] == ["ana", "ana"]
    assert "new conversation" in s.outbox.sent[1]
    assert await s.history.current_conversation_id("diogo") == diogo


async def test_fallback_on_another_users_turn_reaches_the_admin(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([BAD_MODEL, text("gemma's answer")])
    s = Setup(sessions, model, brains=on_bonsai(), users=(DIOGO, ANA))
    await s.queue.enqueue_user("ana", "hi")
    await s.drain()
    assert s.outbox.sent_to == [
        ("ana", notices.fell_back("madam", "bonsai", "gemma")),
        ("diogo", notices.fell_back("sir", "bonsai", "gemma")),
        ("ana", "gemma's answer"),
    ]
    # Not recorded for the admin: the next prompt names the brain.
    assert await s.history.recent("diogo", 10) == []


async def test_fallback_on_the_admins_turn_is_sent_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([BAD_MODEL, text("gemma's answer")])
    s = Setup(sessions, model, brains=on_bonsai(), users=(DIOGO, ANA))
    await s.queue.enqueue_user("diogo", "hi")
    await s.drain()
    assert [to for to, _ in s.outbox.sent_to] == ["diogo", "diogo"]


async def test_typing_in_the_users_own_chat(sessions: async_sessionmaker[AsyncSession]) -> None:
    events: list[str] = []
    s = Setup(sessions, FakeModel([text("hello")]), users=(DIOGO, ANA))
    worker = Worker(
        s.queue,
        s.agent,
        s.history,
        EventOutbox(events),
        QueueConfig(**FAST),
        users=Users((DIOGO, ANA)),
        typing=RecordingTyping(events),
        parking=Parking(NoInputRequests(), NoQuestions()),
        reminders=NoReminders(),
        nudge=ignore,
    )
    await s.queue.enqueue_user("ana", "hi")
    while await worker._step():
        pass
    assert events == ["typing on for ana", "typing off", "send hello"]


async def test_reminder_set_for_another_user(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(sessions, FakeModel([text("Diogo asks you to buy milk, madam.")]), users=(DIOGO, ANA))
    now = datetime.now(UTC)
    rid = await s.reminders.set("buy milk", now, "diogo", ["ana"])
    await s.reminders.claim_due(now)
    await s.drain()
    assert s.outbox.sent_to == [("ana", "Diogo asks you to buy milk, madam.")]
    assert "; set by diogo] buy milk" in user_messages(s.model, 0)[-1]
    ana = await s.history.recent("ana", 30)
    assert [(e.kind, e.reminder_id, e.reminder_set_by) for e in ana] == [
        ("reminder", rid, "diogo"),
        ("reminder_reply", rid, "diogo"),
    ]
    assert await s.history.recent("diogo", 30) == []


async def test_shared_reminder_one_recipient_fails(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    # Claimed in recipient name order: ana's turn first, then diogo's.
    model = FakeModel([ModelFailed("HTTP 400"), text("Dinner with your parents, sir.")])
    s = Setup(sessions, model, users=(DIOGO, ANA))
    now = datetime.now(UTC)
    rid = await s.reminders.set("dinner with my parents", now, "ana", ["ana", "diogo"])
    await s.reminders.claim_due(now)
    await s.drain()
    today = now.astimezone(LISBON).date()
    plain = notices.reminder("madam", "dinner with my parents", now.astimezone(LISBON), today)
    assert s.outbox.sent_to == [("ana", plain), ("diogo", "Dinner with your parents, sir.")]
    assert [i.status for i in await s.items()] == ["failed", "done"]
    assert [(e.kind, e.reminder_set_by) for e in await s.history.recent("diogo", 30)] == [
        ("reminder", "ana"),
        ("reminder_reply", "ana"),
    ]
    assert [e.text for e in await s.history.recent("ana", 30)] == [
        "dinner with my parents",
        plain,
    ]
    assert all(e.reminder_id == rid for e in await s.history.recent("ana", 30))


async def test_reminder_reaches_a_user_without_reminder_tools(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    no_tools = dataclasses.replace(ANA, tools=AllowList.of("cookbook_*"))
    s = Setup(sessions, FakeModel([text("Buy milk, madam.")]), users=(DIOGO, no_tools))
    now = datetime.now(UTC)
    await s.reminders.set("buy milk", now, "diogo", ["ana"])
    await s.reminders.claim_due(now)
    await s.drain()
    assert s.outbox.sent_to == [("ana", "Buy milk, madam.")]
    assert s.model.tool_names(0) == set()


# --- Queue activity for the memory sweeper (change close-idle-conversations, D5) ---


@pytest.mark.parametrize("outcome", ["done", "failed", "unknown user"])
async def test_every_finished_item_is_reported(
    sessions: async_sessionmaker[AsyncSession], outcome: str
) -> None:
    script = {
        "done": [text("hi")],
        "failed": [ModelFailed("HTTP 400")],
        "unknown user": [],
    }[outcome]
    ticks = Ticks()
    s = Setup(sessions, FakeModel(script), clock=ticks)
    await s.queue.enqueue_user("nobody" if outcome == "unknown user" else "diogo", "hi")
    s.queue.activity.changed.clear()
    ticks.now += 50
    await s.drain()
    assert s.queue.activity.changed.is_set()
    assert s.queue.activity.last_finished == ticks.now
    assert [i.status for i in await s.items()] == ["done" if outcome == "done" else "failed"]


# --- Idle close marker on the next message (change close-idle-conversations, D4) ---


async def closed_for_inactivity(s: Setup) -> tuple[int, int]:
    """Make #41 hold an old exchange, then close it as the idle closer would."""
    await start_at_41(s.sessions)
    await s.history.append("diogo", "user", "chicken thighs tonight?")
    await s.history.append("diogo", "assistant", "Lemon and garlic, sir.")
    later = datetime.now(UTC) + timedelta(seconds=1)  # every entry is before it
    reset = await s.history.close_idle(41, later)
    assert reset is not None and reset.archived_id == 41
    return 41, reset.new_id


async def test_idle_marker_before_the_reply(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Setup(sessions, FakeModel([text("Sunny, sir."), text("You're welcome.")]))
    archived, new = await closed_for_inactivity(s)
    await s.queue.enqueue_user("diogo", "morning! what's the weather?")
    await s.drain()
    assert s.outbox.sent == [
        f"— #{archived} closed after inactivity —\n— new conversation #{new} —",
        "Sunny, sir.",
    ]
    assert user_messages(s.model, 0) == ["morning! what's the weather?"]

    await s.queue.enqueue_user("diogo", "thanks")
    await s.drain()
    assert s.outbox.sent[2:] == ["You're welcome."]  # sent once


async def test_ask_and_reminders_do_not_take_the_idle_marker(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    s = Setup(sessions, FakeModel([text("Backup ok."), text("Call mum, sir."), text("Hi.")]))
    archived, new = await closed_for_inactivity(s)
    await s.queue.enqueue_ask("backup", "backup finished", "diogo")
    await due_reminder(s)
    await s.drain()
    assert s.outbox.sent == ["[backup] Backup ok.", "Call mum, sir."]

    await s.queue.enqueue_user("diogo", "hello")
    await s.drain()
    assert s.outbox.sent[2:] == [
        f"— #{archived} closed after inactivity —\n— new conversation #{new} —",
        "Hi.",
    ]


async def test_idle_marker_not_resent_on_a_retry(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    down = ModelUnreachable("refused")
    s = Setup(sessions, FakeModel([down, down, text("Here you go.")]))
    archived, new = await closed_for_inactivity(s)
    await s.queue.enqueue_user("diogo", "what's downloading?")
    await s.drain()
    assert s.outbox.sent == [
        f"— #{archived} closed after inactivity —\n— new conversation #{new} —",
        "Here you go.",
    ]


async def test_no_idle_marker_after_a_user_reset(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await start_at_41(sessions)
    s = Setup(sessions, FakeModel([calls((END_CONVERSATION, "{}")), text("Bye"), text("Hi")]))
    await s.queue.enqueue_user("diogo", "we're done")
    await s.queue.enqueue_user("diogo", "hello again")
    await s.drain()
    assert s.outbox.sent == ["Bye", "— #41 archived —\n— new conversation #42 —", "Hi"]
