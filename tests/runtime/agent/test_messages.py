from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from alfred.features.conversations.store import HistoryEntry
from alfred.runtime.agent.messages import strip_stamp, to_messages

LISBON = ZoneInfo("Europe/Lisbon")
AT = datetime(2026, 11, 9, 14, 5, tzinfo=UTC)  # 14:05 in Lisbon (WET)


def test_message_mapping() -> None:
    entries = [
        HistoryEntry("user", None, "hi"),
        HistoryEntry("assistant", None, "hello"),
        HistoryEntry("ask_prompt", "disk-check", "93% full"),
        HistoryEntry("ask_reply", "disk-check", "Disk is filling up"),
    ]
    assert to_messages(entries) == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "[ask:disk-check] 93% full"},
        {"role": "assistant", "content": "Disk is filling up"},
    ]


def test_recall_without_title() -> None:
    entry = HistoryEntry("recall", None, "transcript", 41)
    assert to_messages([entry]) == [{"role": "user", "content": "[recall:#41] transcript"}]


def test_user_messages_start_with_their_time() -> None:
    entries = [
        HistoryEntry("user", None, "what time is it?", created_at=AT),
        HistoryEntry("assistant", None, "It is just past two.", created_at=AT),
        HistoryEntry("notice", "mcparr", "Dune finished", created_at=AT + timedelta(minutes=15)),
        HistoryEntry("ask_prompt", "disk-check", "93% full", created_at=AT),
        HistoryEntry("ask_reply", "disk-check", "Disk is filling up", created_at=AT),
        HistoryEntry(
            "reminder", None, "call mum", reminder_id=12, reminder_due_at=AT, created_at=AT
        ),
        HistoryEntry("reminder_reply", None, "Your call, sir.", reminder_id=12, created_at=AT),
        HistoryEntry("recall", None, "transcript", 41, "Chicken", created_at=AT),
    ]
    assert [m["content"] for m in to_messages(entries, LISBON)] == [
        "[Mon 9 Nov 14:05] what time is it?",
        "It is just past two.",
        "[Mon 9 Nov 14:20] [notice:mcparr] Dune finished",
        "[Mon 9 Nov 14:05] [ask:disk-check] 93% full",
        "Disk is filling up",
        "[Mon 9 Nov 14:05] [reminder:#12 due Monday 9 November 2026, 14:05] call mum",
        "Your call, sir.",
        '[Mon 9 Nov 14:05] [recall:#41 "Chicken"] transcript',
    ]


def test_message_time_in_the_owners_timezone() -> None:
    entry = HistoryEntry("user", None, "hi", created_at=AT)
    assert to_messages([entry], ZoneInfo("Asia/Tokyo"))[0]["content"] == "[Mon 9 Nov 23:05] hi"


def test_message_times_never_change() -> None:
    # The text depends only on the entry and the zone, never on the time now:
    # a day later it is still "Mon 9 Nov", not "yesterday".
    entry = HistoryEntry("user", None, "hi", created_at=AT)
    assert to_messages([entry], LISBON) == to_messages([entry], LISBON)
    assert to_messages([entry], LISBON)[0]["content"] == "[Mon 9 Nov 14:05] hi"


def test_strip_stamp() -> None:
    assert strip_stamp("[Mon 9 Nov 14:06]  It is two.") == "It is two."
    assert strip_stamp("[Mon 9 Nov 14:06] [notice:x] y") == "[notice:x] y"
    assert strip_stamp("Saved [draft] for later.") == "Saved [draft] for later."
    assert strip_stamp("At [Mon 9 Nov 14:06] it was.") == "At [Mon 9 Nov 14:06] it was."
