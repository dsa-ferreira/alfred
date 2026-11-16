from __future__ import annotations

import dataclasses

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent import notices as agent_notices
from alfred.agent.prompts import _NO_REPLY
from alfred.kernel.access import AllowList
from alfred.kernel.questions import Question
from alfred.kernel.users import User, Users
from alfred.llm.chat import ModelFailed
from alfred.telegram.questions import ended_html
from alfred.turns.config import QueueConfig
from alfred.turns.inputs import notices as inputs_notices
from alfred.turns.inputs.loop import cancel_on_new_message
from alfred.turns.inputs.parking import Parking
from alfred.turns.inputs.store import InputRequests, RequestRow
from alfred.turns.intake import ChatIntake
from alfred.turns.tables import InputRequest
from alfred.turns.worker import Worker
from tests.agent.turn_helpers import tool_rows
from tests.fakes import ignore, no_typing
from tests.helpers import ANA, DIOGO, FakeModel, calls, text
from tests.turns.test_worker import (
    FAST,
    LISBON,
    EventOutbox,
    RecordingTyping,
    Setup,
    due_reminder,
    reminder_notice,
    user_messages,
)

CONFIRMING_DIOGO = dataclasses.replace(DIOGO, confirm=AllowList.of("mcparr_add_media"))
CONFIRMING_ANA = dataclasses.replace(
    ANA, tools=AllowList.of("*"), confirm=AllowList.of("mcparr_add_media")
)
ADD = ("mcparr_add_media", '{"title": "Dune"}')


class FakeQuestions:
    """Records questions and edits; ``failing`` makes sending fail."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.sent: list[tuple[str, int, Question]] = []
        self.edits: list[tuple[str, int, str]] = []
        self.failing = False
        self.events = events

    async def send_question(self, user: User, request_id: int, question: Question) -> int:
        if self.failing:
            raise RuntimeError("Telegram is down")
        self.sent.append((user.name, request_id, question))
        if self.events is not None:
            self.events.append(f"question to {user.name}")
        return 900 + len(self.sent)

    async def edit_question(self, user: User, message_id: int, html: str) -> None:
        self.edits.append((user.name, message_id, html))

    async def ended(self, user: User, request: RequestRow, reason: str | None) -> None:
        """As the Telegram outbox renders it."""
        assert request.telegram_message_id is not None
        await self.edit_question(
            user, request.telegram_message_id, ended_html(request.question, reason)
        )


class Waits(Setup):
    """A Setup whose worker parks turns and sends their questions."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        model: FakeModel,
        users: tuple[User, ...] = (CONFIRMING_DIOGO,),
        typing: RecordingTyping | None = None,
        events: list[str] | None = None,
    ) -> None:
        super().__init__(sessions, model, users=users)
        self.users = Users(users)
        self.requests = InputRequests(sessions, self.history, self.users)
        self.questions = FakeQuestions(events)
        self.chat = ChatIntake(
            sessions,
            self.queue,
            before_message=cancel_on_new_message(self.requests, self.questions, self.users),
        )
        self.worker = self.new_worker(typing=typing, events=events)

    def new_worker(
        self, typing: RecordingTyping | None = None, events: list[str] | None = None
    ) -> Worker:
        return Worker(
            self.queue,
            self.agent,
            self.history,
            EventOutbox(events) if events is not None else self.outbox,
            QueueConfig(**FAST),
            users=self.users,
            zone=LISBON,
            typing=typing or no_typing,
            parking=Parking(self.requests, self.questions),
            nudge=ignore,
        )

    async def tap(self, user: str, option: int = 0, which: int = -1) -> None:
        _, request_id, _ = self.questions.sent[which]
        assert (await self.requests.answer(request_id, option, user)).outcome == "answered"

    async def statuses(self) -> list[str]:
        return [i.status for i in await self.items()]


async def test_others_go_ahead_while_a_turn_waits(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    s = Waits(
        sessions,
        FakeModel([calls(ADD), text("Two downloads, sir.")]),
        users=(DIOGO, CONFIRMING_ANA),
    )
    await s.chat.enqueue_user("ana", "add Dune")
    await s.chat.enqueue_user("diogo", "what's downloading?")
    await s.drain()
    assert await s.statuses() == ["waiting", "done"]
    assert s.outbox.sent_to == [("diogo", "Two downloads, sir.")]
    ((user, _, question),) = s.questions.sent
    assert user == "ana" and question.title == "For your request:"
    assert "mcparr_add_media" in question.body


async def test_waiting_turn_doesnt_block_an_ask(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    s = Waits(sessions, FakeModel([calls(ADD), text("Noted.")]), users=(DIOGO, CONFIRMING_ANA))
    await s.chat.enqueue_user("ana", "add Dune")
    await s.drain()
    await s.queue.enqueue_ask("jellyfin", "anything new?", "diogo")
    await s.drain()
    assert await s.statuses() == ["waiting", "done"]
    assert s.outbox.sent_to == [("diogo", "[jellyfin] Noted.")]


async def test_answered_turn_goes_first(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Waits(
        sessions,
        FakeModel(
            [calls(ADD), text("diogo's answer"), text("Added Dune, madam."), text("Reminder.")]
        ),
        users=(DIOGO, CONFIRMING_ANA),
    )
    await s.chat.enqueue_user("ana", "add Dune")
    await s.worker._step()  # ana's turn parks
    await s.chat.enqueue_user("diogo", "hello")
    await due_reminder(s)  # queued behind diogo's message
    await s.worker._step()  # diogo's turn runs; ana taps meanwhile
    await s.tap("ana")
    await s.drain()
    assert s.outbox.sent_to == [
        ("diogo", "diogo's answer"),
        ("ana", "Added Dune, madam."),
        ("diogo", "Reminder."),
    ]


async def test_reply_after_confirmation(sessions: async_sessionmaker[AsyncSession]) -> None:
    events: list[str] = []
    s = Waits(
        sessions,
        FakeModel([calls(ADD), text("Added Dune, sir.")]),
        typing=RecordingTyping(events),
        events=events,
    )
    await s.chat.enqueue_user("diogo", "add Dune")
    await s.drain()
    events.append("waiting")
    await s.tap("diogo")
    await s.drain()
    # No typing while it waits; shown again from the tap until the reply.
    assert events == [
        "typing on",
        "typing off",
        "question to diogo",
        "waiting",
        "typing on",
        "typing off",
        "send Added Dune, sir.",
    ]
    assert await s.statuses() == ["done"]
    rows = await tool_rows(sessions)
    assert [(r.tool, r.ok) for r in rows] == [
        ("mcparr_add_media", False),
        ("mcparr_add_media", True),
    ]
    async with sessions() as session:
        assert (await session.scalar(select(InputRequest.status))) == "done"
    # The user message is recorded once; the reply after it.
    assert [(e.kind, e.text) for e in await s.history.recent("diogo", 30)] == [
        ("user", "add Dune"),
        ("assistant", "Added Dune, sir."),
    ]


async def test_crash_during_the_confirmed_call(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    s = Waits(sessions, FakeModel([calls(ADD)]))
    await s.chat.enqueue_user("diogo", "add Dune")
    await s.drain()
    await s.tap("diogo")
    # alfred marked it running and stopped before the turn ended.
    (item,) = await s.items()
    assert await s.requests.take_answered(item.id, "diogo") is not None
    restarted = Waits(sessions, FakeModel())  # no reply scripted: the model must not be asked
    await restarted.drain()
    assert await restarted.statuses() == ["failed"]
    assert restarted.outbox.sent == [agent_notices.failed("sir", partial=True)]
    assert [r.ok for r in await tool_rows(sessions)] == [False]  # only the park
    async with sessions() as session:
        assert (await session.scalar(select(InputRequest.status))) == "done"


async def test_restart_while_waiting(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Waits(sessions, FakeModel([calls(ADD)]))
    await s.chat.enqueue_user("diogo", "add Dune")
    await s.drain()
    # A new process: new queue, agent and worker over the same database.
    restarted = Waits(sessions, FakeModel([text("Added Dune, sir.")]))
    _, request_id, _ = s.questions.sent[0]
    assert (await restarted.requests.answer(request_id, 0, "diogo")).outcome == "answered"
    await restarted.drain()
    assert restarted.outbox.sent == ["Added Dune, sir."]
    assert [(r.tool, r.ok) for r in await tool_rows(sessions)] == [
        ("mcparr_add_media", False),
        ("mcparr_add_media", True),
    ]


async def test_resumed_ask_with_no_reply_sends_nothing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    s = Waits(sessions, FakeModel([calls(ADD), text(_NO_REPLY)]))
    await s.queue.enqueue_ask("jellyfin", "add Dune if missing", "diogo")
    await s.drain()
    assert s.questions.sent[0][2].title == "For a request from jellyfin:"
    await s.tap("diogo")
    await s.drain()
    assert s.outbox.sent == []
    assert await s.statuses() == ["done"]


async def test_resumed_reminder_that_fails_sends_the_notice(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    s = Waits(sessions, FakeModel([calls(ADD), ModelFailed("model server answered HTTP 400")]))
    rid = await due_reminder(s)
    due = (await s.reminders.get(rid)).due_at  # type: ignore[union-attr]
    await s.drain()
    assert s.questions.sent[0][2].title == f"For reminder #{rid} (call mum):"
    await s.tap("diogo")
    await s.drain()
    assert s.outbox.sent == [reminder_notice(due)]
    assert await s.statuses() == ["failed"]


async def test_question_not_delivered_cancels_at_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    s = Waits(sessions, FakeModel([calls(ADD)]))
    s.questions.failing = True
    await s.chat.enqueue_user("diogo", "add Dune")
    await s.drain()
    assert await s.statuses() == ["cancelled"]
    assert s.outbox.sent == []
    entries = await s.history.recent("diogo", 30)
    assert entries[-1].text == inputs_notices.input_not_done(
        "sir", "mcparr_add_media", "not_delivered"
    )


async def test_user_moves_on(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Waits(sessions, FakeModel([calls(ADD), text("Two downloads, sir.")]))
    await s.chat.enqueue_user("diogo", "add Dune")
    await s.drain()
    await s.chat.enqueue_user("diogo", "never mind, what's downloading?")
    await s.drain()
    # Only the new message is answered; the waiting turn got no reply or failure notice.
    assert s.outbox.sent == ["Two downloads, sir."]
    assert await s.statuses() == ["cancelled", "done"]
    ((user, message_id, html),) = s.questions.edits
    assert (user, message_id) == ("diogo", 901)
    assert html.endswith("<i>Cancelled: you sent a new message.</i>")
    # The new turn saw that the film was not added.
    seen = user_messages(s.model, 1)
    not_done = inputs_notices.input_not_done("sir", "mcparr_add_media", "new_message")
    assert seen[-2:] == [not_done, "never mind, what's downloading?"]


async def test_tap_after_moving_on_is_refused(sessions: async_sessionmaker[AsyncSession]) -> None:
    s = Waits(sessions, FakeModel([calls(ADD), text("Hello.")]))
    await s.chat.enqueue_user("diogo", "add Dune")
    await s.drain()
    await s.chat.enqueue_user("diogo", "hello")
    _, request_id, _ = s.questions.sent[0]
    assert (await s.requests.answer(request_id, 0, "diogo")).outcome == "not_open"


async def test_an_ask_doesnt_act_on_a_waiting_turns_message(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The 0.16.0 incident: an /ask ran while a chat turn waited, saw the user's
    unanswered message, and asked to add the film as well (0.16.1)."""
    s = Waits(
        sessions,
        FakeModel(
            [
                calls(ADD),  # the chat turn parks
                calls(("mcparr_add_media", '{"title": "Alien"}')),  # the ask asks too
                text("Dune is added, sir."),  # the chat turn, resumed
                text("Sonarr needs a manual import, sir."),  # the ask, resumed
                text("You're welcome."),  # a later message
            ]
        ),
    )
    await s.chat.enqueue_user("diogo", "can you try again pls")
    await s.drain()
    await s.queue.enqueue_ask("mcparr", "Sonarr needs manual intervention", "diogo")
    await s.drain()
    assert await s.statuses() == ["waiting", "waiting"]
    # The ask turn never saw the waiting message.
    assert not any("can you try again" in m for m in user_messages(s.model, 1))

    await s.tap("diogo", 1, which=1)  # cancel the ask's question
    await s.tap("diogo", 0, which=0)  # confirm the chat turn's
    await s.drain()
    assert s.outbox.sent_to == [
        ("diogo", "Dune is added, sir."),
        ("diogo", "[mcparr] Sonarr needs a manual import, sir."),
    ]
    # The ask resumed after the chat turn had ended, still without its message.
    assert not any("can you try again" in m for m in user_messages(s.model, 3))
    # A later turn sees the message with its reply.
    await s.chat.enqueue_user("diogo", "thanks")
    await s.drain()
    seen = user_messages(s.model, 4)
    at = seen.index("can you try again pls")
    assert seen[at + 1] == "Dune is added, sir."
