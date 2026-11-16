"""Reminder tools and reminder turns (change add-reminders)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from mcp.server import MCPServer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.prompts import _ASK_RULE, _NO_REPLY, _REMINDER_ALWAYS, _REMINDER_RULE
from alfred.agent.turn import TurnFailed
from alfred.conversations.store import History
from alfred.conversations.tools import _END_CONVERSATION
from alfred.kernel.turn import AskTurn
from alfred.kernel.users import Users
from alfred.llm.chat import ModelUnreachable
from alfred.reminders.store import Reminders
from alfred.reminders.tools import _REMIND_SET, _REMINDERS_LIST, ReminderTools
from alfred.toolbox.calculator import _CALCULATE
from alfred.toolbox.dates import _DATE
from alfred.turns.inputs.parking import Parking
from tests.agent.turn_helpers import (
    DUE,
    LISBON,
    Clock,
    agent,
    contents,
    owner_turn,
    reminder_tools,
    reminder_turn,
    tool_rows,
)
from tests.fakes import NoInputRequests, NoQuestions, ignore, no_typing
from tests.helpers import (
    ANA,
    DIOGO,
    FakeModel,
    add_queue_item,
    calls,
    make_agent,
    make_config,
    text,
)
from tests.mcp.servers import config as server_config
from tests.mcp.servers import in_process, make_mcparr

# --- reminders -----------------------------------------------------------------


async def test_reminder_tools_offered_in_owner_turns(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock = Clock(DUE)
    model = FakeModel([text("ok")])
    a = agent(sessions, model, reminders=reminder_tools(sessions, clock), now=clock)
    await a.run(await owner_turn(sessions, "hi"))
    assert model.tool_names() >= ReminderTools.names


async def test_reminder_tools_not_offered_in_ask_turns(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock = Clock(DUE)
    model = FakeModel([text(_NO_REPLY), calls((_REMIND_SET, '{"text": "x", "in_minutes": 5}'))])
    model.script.append(text("done"))
    a = agent(sessions, model, reminders=reminder_tools(sessions, clock), now=clock)
    item = await add_queue_item(sessions, "ask", "93%", "disk-check")
    await a.run(AskTurn("93%", item, "disk-check", user=DIOGO))
    assert not ReminderTools.names & model.tool_names(0)
    # A reminder turn gets only the list; setting is refused, nothing stored.
    await a.run(await reminder_turn(sessions))
    assert ReminderTools.names & model.tool_names(1) == {_REMINDERS_LIST}
    assert "unknown tool" in model.requests[2][0][-1]["content"].lower()
    assert await Reminders(sessions).pending("diogo") == []


async def test_owner_sets_a_reminder(sessions: async_sessionmaker[AsyncSession]) -> None:
    clock = Clock(datetime(2026, 11, 9, 16, 47, tzinfo=UTC))
    model = FakeModel(
        [
            calls((_REMIND_SET, '{"text": "check the oven", "in_minutes": 90}')),
            text("Very good. 18:17."),
        ]
    )
    a = agent(sessions, model, reminders=reminder_tools(sessions, clock), now=clock)
    result = await a.run(await owner_turn(sessions, "remind me in 90 minutes to check the oven"))
    assert result.reply == "Very good. 18:17."
    assert model.requests[1][0][-1]["content"].startswith(
        "Reminder set for today at 18:17: check the oven."
    )
    [row] = await tool_rows(sessions)
    assert (row.tool, row.ok) == (_REMIND_SET, True)


async def test_reminder_turn_on_time(sessions: async_sessionmaker[AsyncSession]) -> None:
    history = History(sessions)
    await history.append("diogo", "user", "earlier")
    await history.append("diogo", "assistant", "earlier reply")
    model = FakeModel([text("Your call to mum is due, sir.")])
    a = agent(sessions, model, now=DUE)
    result = await a.run(await reminder_turn(sessions))
    assert result.reply == "Your call to mum is due, sir."
    messages = model.requests[0][0]
    system = messages[0]["content"]
    assert system.endswith(f"{_REMINDER_RULE} {_REMINDER_ALWAYS}")
    assert _ASK_RULE not in system and _NO_REPLY not in system
    assert contents(messages[1:]) == [
        "earlier",
        "earlier reply",
        "[reminder:#12 due Monday 9 November 2026, 17:00; now 17:00] call mum",
    ]


async def test_reminder_turn_gets_the_ask_tool_set_and_the_list(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock = Clock(DUE)
    owner_model = FakeModel([text("ok")])
    reminder_model = FakeModel([text("ok")])
    ask_model = FakeModel([text("ok")])
    tools = reminder_tools(sessions, clock)
    await agent(sessions, owner_model, reminders=tools, now=clock).run(
        await owner_turn(sessions, "hi")
    )
    await agent(sessions, reminder_model, reminders=tools, now=clock).run(
        await reminder_turn(sessions)
    )
    item = await add_queue_item(sessions, "ask", "93%", "disk-check")
    await agent(sessions, ask_model, reminders=tools, now=clock).run(
        AskTurn("93%", item, "disk-check", user=DIOGO)
    )
    assert reminder_model.tool_names() == ask_model.tool_names() | {_REMINDERS_LIST}
    assert _END_CONVERSATION not in reminder_model.tool_names()
    assert reminder_model.tool_names() < owner_model.tool_names()


async def test_reminder_turn_late_retry_shows_the_new_now(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock = Clock(DUE + timedelta(minutes=12))
    model = FakeModel([ModelUnreachable("ai.lan asleep"), text("Rather late, sir: call mum.")])
    a = agent(sessions, model, now=clock)
    turn = await reminder_turn(sessions)
    with pytest.raises(TurnFailed) as failed:
        await a.run(turn)
    assert failed.value.retryable
    assert model.requests[0][0][-1]["content"].startswith(
        "[reminder:#12 due Monday 9 November 2026, 17:00; now 17:12]"
    )
    clock.now = DUE + timedelta(hours=2, minutes=40)
    await a.run(turn)
    assert model.requests[1][0][-1]["content"] == (
        "[reminder:#12 due Monday 9 November 2026, 17:00; now 19:40] call mum"
    )


async def test_reminder_turn_days_late_shows_the_date(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([text("ok")])
    await agent(sessions, model, now=DUE + timedelta(days=3)).run(await reminder_turn(sessions))
    assert model.requests[0][0][-1]["content"] == (
        "[reminder:#12 due Monday 9 November 2026, 17:00; now Thursday 12 November 2026, 17:00]"
        " call mum"
    )


async def test_reminder_tool_failure_reaches_the_model(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    class Broken(ReminderTools):
        def __init__(self) -> None:
            super().__init__(Reminders(sessions), LISBON, "09:00", Users((DIOGO,)))

        async def call(self, name: str, raw_arguments: object, attempt: object = None) -> object:
            raise OSError("database down")

    model = FakeModel([calls((_REMIND_SET, '{"text": "x", "in_minutes": 5}')), text("Sorry.")])
    a = agent(sessions, model, reminders=Broken())  # type: ignore[arg-type]
    await a.run(await owner_turn(sessions, "remind me in 5 minutes"))
    assert "failed: OSError" in model.requests[1][0][-1]["content"]


async def test_reminder_turn_offers_calculator_and_date_before_mcp(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([text("ok")])
    await agent(sessions, model, now=DUE).run(await reminder_turn(sessions))
    names = [t["function"]["name"] for t in model.requests[0][1]]
    assert names.index(_CALCULATE) < names.index(_DATE) < names.index("mcparr_get_downloads")


# --- Set by someone else (change add-multiple-users) ----------------------------------


async def test_reminder_set_by_someone_else(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([text("Diogo asks you to buy milk, madam.")])
    a = agent(sessions, model, now=DUE, users=[DIOGO, ANA])
    await a.run(await reminder_turn(sessions, 14, "buy milk", user=ANA, set_by="diogo"))
    messages = model.requests[0][0]
    assert contents(messages[1:]) == [
        "[reminder:#14 due Monday 9 November 2026, 17:00; now 17:00; set by diogo] buy milk"
    ]
    system = messages[0]["content"]
    assert "You are chatting over Telegram with ana, the user." in system
    assert 'Address the user as "madam"' in system


# --- Scheduled briefings (change add-recurring-reminders) -------------------------------


def make_weather() -> MCPServer:
    server = MCPServer("weather")

    @server.tool()
    def forecast() -> str:
        """Today's forecast."""
        return "Lisbon: sunny, 18 degrees"

    return server


async def test_morning_briefing_uses_the_tools(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    from alfred.reminders.when import parse_rule
    from alfred.turns.queue import Queue
    from alfred.turns.worker import Worker
    from tests.turns.test_worker import FakeOutbox

    monday = datetime(2026, 11, 16, 8, 0, tzinfo=LISBON)
    cfg = make_config(
        mcp_servers=[server_config("weather"), server_config("mcparr")],
        queue={"retry_initial_s": 0.01},
    )
    store = Reminders(sessions, LISBON)
    tools = ReminderTools(store, LISBON, "09:00", cfg.users, now=lambda: monday)
    history = History(sessions)
    model = FakeModel(
        [
            calls(
                ("weather_forecast", "{}"), ("mcparr_get_downloads", "{}"), (_REMINDERS_LIST, "{}")
            ),
            text("Good morning, sir. Sunny, 18 degrees; Dune is half done; the dentist at 17:00."),
        ]
    )
    connect = in_process({"weather": make_weather(), "mcparr": make_mcparr()})
    a = make_agent(cfg, model, history, sessions, connect, reminders=tools, now=lambda: monday)
    queue, outbox = Queue(sessions), FakeOutbox()
    worker = Worker(
        queue,
        a,
        history,
        outbox,
        cfg.queue,
        users=cfg.users,
        reminders=store,
        zone=LISBON,
        parking=Parking(NoInputRequests(), NoQuestions()),
        typing=no_typing,
        nudge=ignore,
    )
    rule = parse_rule(repeat="weekdays", at="08:00")
    briefing = await store.set(
        "brief me on the weather, what's downloading and my reminders for today",
        monday,
        "diogo",
        ["diogo"],
        rule=rule,
    )
    await store.set("dentist", monday.replace(hour=17), "diogo", ["diogo"])
    await store.claim_due(monday)
    while await worker._step():
        pass

    reply = "Good morning, sir. Sunny, 18 degrees; Dune is half done; the dentist at 17:00."
    assert outbox.sent == [reply]
    tool_results = [m["content"] for m in model.requests[1][0] if m["role"] == "tool"]
    assert "sunny, 18 degrees" in tool_results[0]
    assert "Dune" in tool_results[1]
    assert "dentist" in tool_results[2] and "every weekday at 08:00" in tool_results[2]
    assert [(e.kind, e.reminder_id, e.text) for e in await history.recent("diogo", 10)] == [
        (
            "reminder",
            briefing,
            "brief me on the weather, what's downloading and my reminders for today",
        ),
        ("reminder_reply", briefing, reply),
    ]
    assert [r.ok for r in await tool_rows(sessions)] == [True, True, True]


async def test_scheduled_briefing_is_set_not_done(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    sunday = datetime(2026, 11, 15, 14, 47, tzinfo=LISBON)
    clock = Clock(sunday)
    arguments = '{"text": "brief me on the weather", "repeat": "weekdays", "at": "08:00"}'
    model = FakeModel([calls((_REMIND_SET, arguments)), text("Very good. Each weekday at eight.")])
    a = agent(sessions, model, reminders=reminder_tools(sessions, clock), now=clock)
    await a.run(await owner_turn(sessions, "every weekday at 8, brief me on the weather"))
    system = model.requests[0][0][0]["content"]
    assert "every weekday at 8, brief me on the weather" in system.replace("\n", " ").lower()
    [row] = await Reminders(sessions).pending("diogo")
    assert row.rule is not None and row.rule.label() == "every weekday at 08:00"
    assert row.due_at == datetime(2026, 11, 16, 8, 0, tzinfo=LISBON)
    assert [r.tool for r in await tool_rows(sessions)] == [_REMIND_SET]
