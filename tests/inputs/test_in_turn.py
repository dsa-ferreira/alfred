"""Whole flows: a turn waits, the user taps a button in Telegram, the turn goes on.

The queue, worker, agent, request store, tap handler and outbox are wired as
``serve`` wires them, over a fake Telegram bot and in-process MCP servers
(change add-user-input-requests).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any

from aiogram import Dispatcher
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from mcp.server import MCPServer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.config import QueueConfig, User, Users
from alfred.history import History
from alfred.inputs.callbacks import Taps
from alfred.inputs.loop import cancel_on_new_message
from alfred.inputs.store import InputRequests
from alfred.queue import Queue, Worker
from alfred.reminders.store import Reminders
from alfred.reminders.tools import _REMIND_SET, ReminderTools
from alfred.telegram.bot import TelegramOutbox, make_dispatcher
from alfred.tools.base import AllowList
from tests.agent.turn_helpers import LISBON, tool_rows
from tests.helpers import ANA, ANA_ID, DIOGO, OWNER, FakeModel, calls, make_agent, make_config, text
from tests.mcp.servers import config as server_config
from tests.mcp.servers import in_process, make_asking
from tests.telegram.fake_bot import FakeSession, callback_update, make_bot

ANA_CONFIRMS = dataclasses.replace(
    ANA, tools=AllowList.of("*"), confirm=AllowList.of("mcparr_add_media")
)
DIOGO_CONFIRMS = dataclasses.replace(DIOGO, confirm=AllowList.of("mcparr_add_media"))
USERS = Users((DIOGO_CONFIRMS, ANA_CONFIRMS))


def counting_mcparr(added: list[str]) -> MCPServer:
    server = MCPServer("mcparr")

    @server.tool()
    def add_media(title: str) -> str:
        """Add a movie or series."""
        added.append(title)
        return f"added {title}"

    return server


class Alfred:
    """serve's wiring of a turn that waits, around a fake bot."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        model: FakeModel,
        server: MCPServer,
        users: Users = USERS,
    ) -> None:
        self.bot, self.session = make_bot()
        self.reminders = Reminders(sessions)
        config = make_config(mcp_servers=[server_config("mcparr")], users=list(users))
        history = History(sessions)
        self.queue = Queue(sessions)
        outbox = TelegramOutbox(self.bot)
        self.requests = InputRequests(sessions, history, users)
        self.queue.before_user_message = cancel_on_new_message(self.requests, outbox, users)
        agent = make_agent(
            config,
            model,
            history,
            sessions,
            in_process({"mcparr": server}),
            reminders=ReminderTools(self.reminders, LISBON, "09:00", users),
        )
        self.worker = Worker(
            self.queue,
            agent,
            history,
            outbox,
            QueueConfig(retry_initial_s=0.01, retry_max_s=0.04),
            users=users,
            reminders=self.reminders,
            zone=LISBON,
            inputs=self.requests,
            questions=outbox,
        )
        self.dispatcher: Dispatcher = make_dispatcher(
            users,
            self.queue,
            on_tap=Taps(users, self.requests, outbox, self.queue.activity.notify),
        )

    async def drain(self) -> None:
        while await self.worker.step():
            pass

    def questions(self) -> list[SendMessage]:
        return [m for m in self.session.sent() if m.reply_markup is not None]

    async def tap(self, user: User, label: str) -> None:
        """``user`` taps the button ``label`` on the newest question."""
        question = self.questions()[-1]
        rows = question.reply_markup.inline_keyboard  # type: ignore[union-attr]
        data = next(b.callback_data for row in rows for b in row if b.text == label)
        assert data is not None
        message_id = self.session.requests.index(question) + 1  # FakeSession's numbering
        update = callback_update(data, user_id=user.telegram_user_id, message_id=message_id)
        await self.dispatcher.feed_update(self.bot, update)

    def replies_to(self, user_id: int) -> list[str]:
        return [
            m.text
            for m in self.session.sent()
            if int(m.chat_id) == user_id and m.reply_markup is None
        ]


def requests_of(session: FakeSession, kind: type) -> list[Any]:
    return [r for r in session.requests if isinstance(r, kind)]


async def test_confirmed_call_then_a_reminder(sessions: async_sessionmaker[AsyncSession]) -> None:
    added: list[str] = []
    model = FakeModel(
        [
            calls(
                ("mcparr_add_media", '{"title": "Dune"}'),
                (_REMIND_SET, '{"text": "watch Dune", "in_minutes": 60}'),
            ),
            text("Dune is added, and I shall remind you in an hour, madam."),
        ]
    )
    alfred = Alfred(sessions, model, counting_mcparr(added))
    await alfred.queue.enqueue_user("ana", "add Dune and remind me to watch it")
    await alfred.drain()

    (question,) = alfred.questions()
    assert int(question.chat_id) == ANA_ID
    assert "mcparr_add_media" in question.text and "Dune" in question.text
    assert added == [] and await alfred.reminders.pending("ana") == []

    await alfred.tap(ANA_CONFIRMS, "Do it")
    assert requests_of(alfred.session, AnswerCallbackQuery)[0].text == "Do it"
    assert requests_of(alfred.session, EditMessageText)[0].text.endswith("<i>Confirmed.</i>")
    await alfred.drain()

    assert added == ["Dune"]  # once
    assert len(await alfred.reminders.pending("ana")) == 1
    assert alfred.replies_to(ANA_ID) == ["Dune is added, and I shall remind you in an hour, madam."]
    assert alfred.replies_to(OWNER) == []
    assert [(r.tool, r.ok) for r in await tool_rows(sessions)] == [
        ("mcparr_add_media", False),
        ("mcparr_add_media", True),
        (_REMIND_SET, True),
    ]


async def test_server_question_answered_yes(sessions: async_sessionmaker[AsyncSession]) -> None:
    ran: list[str] = []
    # diogo's confirm list doesn't name the tool: the server itself asks.
    model = FakeModel(
        [calls(("mcparr_add_media", '{"title": "Dune"}')), text("Dune is added, sir.")]
    )
    alfred = Alfred(sessions, model, make_asking(ran=ran), Users((DIOGO, ANA_CONFIRMS)))
    await alfred.queue.enqueue_user("diogo", "add Dune")
    await alfred.drain()

    (question,) = alfred.questions()
    assert "mcparr asks: Add &#x27;Dune&#x27; to the library?" in question.text
    assert ran == []
    await alfred.tap(DIOGO, "Yes")
    await alfred.drain()
    assert ran == ["add_media"]
    assert alfred.replies_to(OWNER) == ["Dune is added, sir."]
    assert "added Dune" in model.requests[1][0][-1]["content"]


async def test_reminder_needs_confirmation(sessions: async_sessionmaker[AsyncSession]) -> None:
    added: list[str] = []
    model = FakeModel(
        [
            calls(("mcparr_add_media", '{"title": "Dune"}')),
            text("As you asked, sir, Dune is now in the library."),
        ]
    )
    alfred = Alfred(sessions, model, counting_mcparr(added))
    now = datetime.now(UTC)
    rid = await alfred.reminders.set("add Dune to the library", now, "diogo", ["diogo"])
    assert len(await alfred.reminders.claim_due(now)) == 1
    await alfred.drain()

    (question,) = alfred.questions()
    assert int(question.chat_id) == OWNER
    assert f"For reminder #{rid} (add Dune to the library):" in question.text
    await alfred.tap(DIOGO_CONFIRMS, "Do it")
    await alfred.drain()
    assert added == ["Dune"]
    assert alfred.replies_to(OWNER) == ["As you asked, sir, Dune is now in the library."]
    entries = await History(sessions).recent("diogo", 30)
    assert [(e.kind, e.text) for e in entries] == [
        ("reminder", "add Dune to the library"),
        ("reminder_reply", "As you asked, sir, Dune is now in the library."),
    ]
