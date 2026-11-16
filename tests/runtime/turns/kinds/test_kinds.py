"""Turn kinds' decisions, cell by cell of the turn-kinds design's Context table.

Every decision is pure, so these tests use no fakes: a queue item, a user and
a time go in, a decision comes out.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from alfred.features.conversations.tables import EntryOrigin
from alfred.features.reminders import notices as reminders_notices
from alfred.kernel.turn import AskTurn, ChatTurn, ReminderTurn, TurnInput
from alfred.runtime.agent.framing import TurnRules
from alfred.runtime.agent.results import TurnFailed
from alfred.runtime.agent.turn import _Times
from alfred.runtime.turns.kinds import Kinds
from alfred.runtime.turns.kinds.ask import AskKind, AskPayload
from alfred.runtime.turns.kinds.base import Deliver, NewEntry, Opening, Silent, Tell, TurnKind
from alfred.runtime.turns.kinds.chat import ChatKind, ChatPayload
from alfred.runtime.turns.kinds.reminder import ReminderKind, ReminderPayload
from alfred.runtime.turns.queue import Item
from tests.helpers import DIOGO

LISBON = ZoneInfo("Europe/Lisbon")
DUE = datetime(2026, 11, 9, 17, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 9, 17, 12, tzinfo=UTC)
KINDS = Kinds.default(LISBON)
FAILED = TurnFailed(
    "boom", "I could not finish that, sir.", retryable=False, side_effects_possible=False
)


def item(payload: Any, item_id: int = 7) -> Item[Any]:
    return Item(item_id, DIOGO.name, payload, 0, NOW, NOW)


CHAT = item(ChatPayload("what's this?", (3, 4)))
ASK = item(AskPayload("disk-check", "/mnt/data at 93%"))
REMINDER = item(ReminderPayload(12, DUE, "call mum", "diogo", "every day at 17:00"))
QUIET = item(ReminderPayload(12, DUE, "brief me", "ana", quiet_ok=True))


def built(queued: Item[Any]) -> tuple[TurnKind[Any, Any], TurnInput]:
    kind = KINDS.by_tag(queued.kind)
    return kind, kind.turn(queued, DIOGO)


def notice(text: str) -> str:
    return reminders_notices.reminder("sir", text, DUE.astimezone(LISBON), NOW.date())


# --- the registry ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("tag", "kind_type", "turn"),
    [
        ("chat", ChatKind, ChatTurn("hi", 1, user=DIOGO)),
        ("ask", AskKind, AskTurn("hi", 1, "backup", user=DIOGO)),
        ("reminder", ReminderKind, ReminderTurn("hi", 1, user=DIOGO)),
    ],
)
def test_registry_finds_each_kind_by_tag_and_turn_type(
    tag: str, kind_type: type, turn: TurnInput
) -> None:
    assert isinstance(KINDS.by_tag(tag), kind_type)
    assert KINDS.of(turn) is KINDS.by_tag(tag)


def test_the_turn_each_kind_builds() -> None:
    assert built(CHAT)[1] == ChatTurn("what's this?", 7, user=DIOGO)
    assert built(ASK)[1] == AskTurn("/mnt/data at 93%", 7, "disk-check", user=DIOGO)
    assert built(REMINDER)[1] == ReminderTurn(
        "call mum", 7, reminder_id=12, due_at=DUE, repeat_label="every day at 17:00", user=DIOGO
    )
    # Set by someone else: named.
    assert built(QUIET)[1] == ReminderTurn(
        "brief me", 7, reminder_id=12, due_at=DUE, set_by="ana", quiet_ok=True, user=DIOGO
    )


# --- start and typing ------------------------------------------------------------


def test_opening() -> None:
    kind, turn = built(CHAT)
    assert kind.opening(CHAT, turn) == Opening(
        (NewEntry("user", "what's this?", attachment_ids=(3, 4)),), idle_marker=True
    )
    for queued in (ASK, REMINDER):
        kind, turn = built(queued)
        assert kind.opening(queued, turn) == Opening()


def test_typing() -> None:
    assert [KINDS.by_tag(t).shows_typing for t in ("chat", "ask", "reminder")] == [
        True,
        False,
        False,
    ]


# --- reply -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("queued", "reply", "answer"),
    [
        (CHAT, "It's a receipt.", "It's a receipt."),
        (ASK, "Shall I clean up?", "Shall I clean up?"),
        (ASK, "NO_REPLY", None),
        (REMINDER, "Time to call mum, sir.", "Time to call mum, sir."),
        (REMINDER, "  ", notice("call mum")),
        (REMINDER, "NO_REPLY", notice("call mum")),
        (QUIET, "NO_REPLY", None),
        (QUIET, "", notice("brief me")),
    ],
)
def test_answer(queued: Item[Any], reply: str, answer: str | None) -> None:
    kind, turn = built(queued)
    assert kind.answer(turn, reply, NOW) == answer


# --- failure ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("queued", "ending"),
    [
        (CHAT, Tell(FAILED.notice)),
        (ASK, Silent()),
        (REMINDER, Deliver(notice("call mum"))),
        # A broken AI doesn't look like "nothing to report".
        (QUIET, Deliver(notice("brief me"))),
    ],
)
def test_failure_ending(queued: Item[Any], ending: object) -> None:
    kind, turn = built(queued)
    assert kind.on_failure(turn, FAILED, NOW) == ending


def test_reminder_notice_names_another_day() -> None:
    kind, turn = built(REMINDER)
    later = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
    ending = kind.on_failure(turn, FAILED, later)
    assert isinstance(ending, Deliver) and "on Monday 9 November" in ending.text


# --- what the exchange records and sends (replies and unanswered questions) -------


def test_entries_and_message() -> None:
    kind, turn = built(CHAT)
    assert kind.entries(turn, "Done.") == [NewEntry("assistant", "Done.")]
    assert kind.message(turn, "Done.") == "Done."

    kind, turn = built(ASK)
    assert kind.entries(turn, "Done.") == [
        NewEntry("ask_prompt", "/mnt/data at 93%", source="disk-check"),
        NewEntry("ask_reply", "Done.", source="disk-check"),
    ]
    assert kind.message(turn, "Done.") == "[disk-check] Done."

    for queued, text, created_by in ((REMINDER, "call mum", "diogo"), (QUIET, "brief me", "ana")):
        kind, turn = built(queued)
        origin = EntryOrigin(12, DUE, created_by)
        assert kind.entries(turn, "Done.") == [
            NewEntry("reminder", text, origin=origin),
            NewEntry("reminder_reply", "Done.", origin=origin),
        ]
        assert kind.message(turn, "Done.") == "Done."


# --- question title --------------------------------------------------------------


def test_question_titles() -> None:
    assert [k.question_title(t) for k, t in map(built, (CHAT, ASK, REMINDER))] == [
        "For your request:",
        "For a request from disk-check:",
        "For reminder #12 (call mum):",
    ]
    long = item(ReminderPayload(3, DUE, "x" * 61, "diogo"))
    kind, turn = built(long)
    assert kind.question_title(turn) == f"For reminder #3 ({'x' * 59}…):"


# --- prompt ----------------------------------------------------------------------


def test_framing() -> None:
    times = _Times(LISBON)
    chat = KINDS.of(t := built(CHAT)[1]).framing(t)
    assert (chat.rules, chat.inline_attachments, chat.input_line(NOW, times)) == (
        TurnRules(),
        True,
        None,
    )
    ask = KINDS.of(t := built(ASK)[1]).framing(t)
    assert (ask.rules, ask.inline_attachments) == (TurnRules(ask=True), False)
    assert ask.input_line(NOW, times) == times.stamp("[ask:disk-check] /mnt/data at 93%", NOW)

    reminder = KINDS.of(t := built(REMINDER)[1]).framing(t)
    assert (reminder.rules, reminder.inline_attachments) == (TurnRules(reminder=True), False)
    assert reminder.input_line(NOW, times) == (
        f"[reminder:#12 due {times.when(DUE)}; now {times.now(NOW, DUE)}; "
        "every day at 17:00] call mum"
    )
    quiet = KINDS.of(t := built(QUIET)[1]).framing(t)
    assert quiet.rules == TurnRules(reminder=True, reminder_quiet=True)
    assert quiet.input_line(NOW, times).endswith("; set by ana] brief me")  # type: ignore[union-attr]
