"""Queue payloads round-trip through JSON (design D4)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from alfred.turns.kinds import payload_from_json, tag_of
from alfred.turns.kinds.ask import AskPayload
from alfred.turns.kinds.chat import ChatPayload
from alfred.turns.kinds.reminder import ReminderPayload

Payload = ChatPayload | AskPayload | ReminderPayload

DUE = datetime(2026, 11, 16, 8, 0, tzinfo=ZoneInfo("Europe/Lisbon"))


@pytest.mark.parametrize(
    "payload",
    [
        ChatPayload("what's this?", (7, 8)),
        ChatPayload(""),
        AskPayload("disk-check", "/mnt/data at 93%"),
        ReminderPayload(12, DUE, "brief me", "ana", "every weekday at 08:00", quiet_ok=True),
        ReminderPayload(3, datetime(2026, 11, 9, 17, 0, tzinfo=UTC), "call mum", "diogo"),
    ],
)
def test_round_trip(payload: Payload) -> None:
    # Through text, as JSONB stores it.
    stored = json.loads(json.dumps(payload.to_json()))
    assert payload_from_json(tag_of(payload), stored) == payload


def test_tags() -> None:
    tags = [
        tag_of(ChatPayload("")),
        tag_of(AskPayload("s", "p")),
        tag_of(ReminderPayload(1, DUE, "x", "d")),
    ]
    assert tags == ["chat", "ask", "reminder"]


def test_due_time_keeps_its_instant() -> None:
    stored = json.loads(json.dumps(ReminderPayload(1, DUE, "x", "d").to_json()))
    assert ReminderPayload.from_json(stored).due_at == DUE


def test_unknown_kind() -> None:
    with pytest.raises(ValueError, match="'user'"):
        payload_from_json("user", {"text": "hi"})
