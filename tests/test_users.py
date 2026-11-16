"""Two users end to end (change add-multiple-users): offered tools, isolation, reminders.

``diogo`` is the admin with every tool; ``ana`` has ``alfred_*``, ``cookbook_*``
and two mcparr tools. Their messages go through the real worker, agent,
toolsets, memory and reminders.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.features.archive.search import ConversationSearch
from alfred.features.archive.tools import _RECALL_CONVERSATION, _SEARCH_CONVERSATIONS
from alfred.features.conversations.store import History
from alfred.features.conversations.tools import _END_CONVERSATION
from alfred.features.reminders.store import Reminders
from alfred.features.reminders.tools import _REMIND_SET, _REMINDERS_LIST, ReminderTools
from alfred.kernel.users import User
from alfred.runtime.agent.messages import strip_stamp
from alfred.runtime.turns.inputs.parking import Parking
from alfred.runtime.turns.intake import ChatIntake
from alfred.runtime.turns.kinds import Kinds
from alfred.runtime.turns.queue import Queue
from alfred.runtime.turns.worker import Worker
from tests.fakes import NoInputRequests, NoQuestions, ignore, no_typing
from tests.features.mcp.servers import in_process, make_cookbook, make_mcparr
from tests.helpers import (
    ANA,
    DIOGO,
    FakeEmbedder,
    FakeModel,
    calls,
    claim_due,
    make_agent,
    make_config,
    text,
)
from tests.runtime.agent.turn_helpers import COOKBOOK, MCPARR, Clock

LISBON = ZoneInfo("Europe/Lisbon")
START = datetime(2026, 11, 9, 16, 0, tzinfo=UTC)  # 16:00 in Lisbon


class Outbox:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, user: User, text_: str) -> None:
        self.sent.append((user.name, text_))


class House:
    """alfred serving diogo and ana, as ``serve`` wires it."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], model: FakeModel) -> None:
        cfg = make_config(mcp_servers=[MCPARR, COOKBOOK], users=[DIOGO, ANA])
        self.clock = Clock(START)
        self.model = model
        self.history = History(sessions)
        self.sessions = sessions
        self.queue = Queue(sessions)
        self.outbox = Outbox()
        self.reminders = Reminders(sessions)
        self.search = ConversationSearch(
            cfg.memory, ZoneInfo(cfg.ai.timezone), sessions, FakeEmbedder(default=[1.0])
        )
        tools = ReminderTools(self.reminders, LISBON, "09:00", cfg.users, now=self.clock)
        connect = in_process({"mcparr": make_mcparr(), "cookbook": make_cookbook()})
        agent = make_agent(
            cfg,
            model,
            self.history,
            sessions,
            connect,
            memory=self.search,
            reminders=tools,
            now=self.clock,
        )
        self.worker = Worker(
            self.queue,
            agent,
            self.history,
            self.outbox,
            cfg.queue,
            users=cfg.users,
            kinds=Kinds.default(LISBON),
            parking=Parking(NoInputRequests(), NoQuestions()),
            typing=no_typing,
            nudge=ignore,
        )

    async def say(self, user: str, message: str) -> None:
        await ChatIntake(self.sessions, self.queue).enqueue_user(user, message)
        while await self.worker._step():
            pass


def offered(model: FakeModel, request: int) -> set[str]:
    return model.tool_names(request)


def context(model: FakeModel, request: int) -> list[str]:
    return [strip_stamp(m["content"]) for m in model.requests[request][0][1:]]


async def test_two_users(sessions: async_sessionmaker[AsyncSession]) -> None:
    remind_ana = {"text": "buy milk", "at": "18:00", "for": ["ana"]}
    model = FakeModel(
        [
            # 0, 1: diogo talks about chicken, then archives the conversation.
            calls((_END_CONVERSATION, "{}")),
            text("Very good, sir."),
            # 2, 3: diogo reminds ana.
            calls((_REMIND_SET, json.dumps(remind_ana))),
            text("Very good. I shall remind ana at six."),
            # 4, 5: ana searches her memory for chicken: nothing of hers.
            calls((_SEARCH_CONVERSATIONS, json.dumps({"query": "chicken"}))),
            text("I find nothing, madam."),
            # 6, 7: ana tries to recall diogo's conversation by number.
            calls((_RECALL_CONVERSATION, json.dumps({"id": 1}))),
            text("There is no such conversation, madam."),
            # 8, 9: ana lists her reminders.
            calls((_REMINDERS_LIST, "{}")),
            text("Milk at six, set by diogo."),
            # 10: the reminder turn for ana.
            text("Pardon the interruption, madam. Diogo asks you to buy milk."),
        ]
    )
    house = House(sessions, model)

    await house.say("diogo", "the chicken thighs recipe, 40 minutes at 200C. we're done")
    await house.say("diogo", "remind ana to buy milk at six")
    await house.say("ana", "remember that chicken recipe?")
    await house.say("ana", "remember conversation 1")
    await house.say("ana", "what reminders do I have?")

    # Offered tools: diogo gets everything; ana her allow-list only.
    diogo_tools, ana_tools = offered(model, 0), offered(model, 4)
    assert {"mcparr_get_downloads", "cookbook_delete_recipe"} <= diogo_tools
    assert not {"mcparr_get_downloads", "mcparr_check_health"} & ana_tools
    assert {"mcparr_add_media", "cookbook_get_recipe", _REMIND_SET, _SEARCH_CONVERSATIONS} <= (
        ana_tools
    )

    # Prompts are built for each user.
    diogo_prompt = model.requests[0][0][0]["content"]
    ana_prompt = model.requests[4][0][0]["content"]
    assert "with diogo, the user" in diogo_prompt and '"sir"' in diogo_prompt
    assert "with ana, the user" in ana_prompt and '"madam"' in ana_prompt
    assert "The other users who can be reminded: diogo." in " ".join(ana_prompt.split())

    # History: ana's context holds none of diogo's messages.
    assert context(model, 4) == ["remember that chicken recipe?"]
    assert not any("chicken thighs" in m for m in context(model, 8))

    # Memory: ana finds nothing of diogo's, and can't recall it by number.
    assert "No archived conversations match." in context(model, 5)[-1]
    assert context(model, 7)[-1] == "Error: there is no conversation #1."
    diogo_search = await house.search.search("diogo", "chicken")
    assert "#1" in diogo_search.text

    # The cross-user reminder: listed for ana as set by diogo, delivered to her alone.
    assert "buy milk (set by diogo; for ana)" in context(model, 9)[-1]
    house.clock.now = START + timedelta(hours=2)  # 18:00 Lisbon
    assert len(await claim_due(house.reminders, house.clock.now)) == 1
    while await house.worker._step():
        pass
    assert context(model, 10)[-1].endswith("; set by diogo] buy milk")
    assert house.outbox.sent[-1] == (
        "ana",
        "Pardon the interruption, madam. Diogo asks you to buy milk.",
    )
    assert [to for to, _ in house.outbox.sent] == [
        "diogo",
        "diogo",
        "diogo",
        "ana",
        "ana",
        "ana",
        "ana",
    ]
    ana_entries = await house.history.recent("ana", 30)
    assert [e.kind for e in ana_entries][-2:] == ["reminder", "reminder_reply"]
    diogo_entries = await house.history.recent("diogo", 30)
    assert all(e.kind != "reminder" for e in diogo_entries)
