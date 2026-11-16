"""Constraints of the conversations tables."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from alfred.conversations.tables import EntryOrigin


def test_one_current_conversation_per_user(conn: Connection) -> None:
    conn.execute(text("INSERT INTO conversations (user_name) VALUES ('diogo')"))
    # An archived one is fine, and so is another user's current one...
    conn.execute(text("INSERT INTO conversations (user_name, ended_at) VALUES ('diogo', now())"))
    conn.execute(text("INSERT INTO conversations (user_name) VALUES ('ana')"))
    # ...a second current one for the same user is not.
    with pytest.raises(IntegrityError):
        conn.execute(text("INSERT INTO conversations (user_name) VALUES ('diogo')"))


def test_end_reason_is_checked(conn: Connection) -> None:
    for reason in ("user", "idle", "unattended"):
        conn.execute(
            text(
                "INSERT INTO conversations (user_name, ended_at, end_reason) "
                "VALUES ('d', now(), :r)"
            ),
            {"r": reason},
        )
    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO conversations (user_name, ended_at, end_reason) "
                "VALUES ('d', now(), 'bored')"
            )
        )


def test_recall_not_twice_into_one_conversation(conn: Connection) -> None:
    conn.execute(
        text("INSERT INTO conversations (id, user_name, ended_at) VALUES (41, 'd', now())")
    )
    conn.execute(text("INSERT INTO conversations (id, user_name) VALUES (42, 'd')"))
    insert = text(
        "INSERT INTO entries (conversation_id, kind, text, recalled_conversation_id) "
        "VALUES (42, 'recall', 'x', 41)"
    )
    conn.execute(insert)
    with pytest.raises(IntegrityError):
        conn.execute(insert)


def test_a_turn_records_each_kind_once(conn: Connection) -> None:
    conn.execute(text("INSERT INTO conversations (id, user_name) VALUES (1, 'd')"))
    insert = (
        "INSERT INTO entries (conversation_id, kind, text, turn_id) VALUES (1, :kind, 'x', :turn)"
    )
    conn.execute(text(insert), {"kind": "user", "turn": 7})
    conn.execute(text(insert), {"kind": "assistant", "turn": 7})
    # Entries of no turn (notices) don't collide.
    conn.execute(text(insert), {"kind": "notice", "turn": None})
    conn.execute(text(insert), {"kind": "notice", "turn": None})
    with pytest.raises(IntegrityError):
        conn.execute(text(insert), {"kind": "user", "turn": 7})


def test_entry_kind_is_checked(conn: Connection) -> None:
    conn.execute(text("INSERT INTO conversations (id, user_name) VALUES (1, 'd')"))
    with pytest.raises(IntegrityError):
        conn.execute(
            text("INSERT INTO entries (conversation_id, kind, text) VALUES (1, 'owner', 'x')")
        )


def test_entry_attachments_need_both_rows(conn: Connection) -> None:
    conn.execute(text("INSERT INTO conversations (id, user_name) VALUES (1, 'd')"))
    conn.execute(
        text("INSERT INTO entries (id, conversation_id, kind, text) VALUES (5, 1, 'user', 'x')")
    )
    conn.execute(
        text(
            "INSERT INTO attachments (id, user_name, kind, mime, size_bytes, object_key) "
            "VALUES (9, 'd', 'image', 'image/jpeg', 1, 'attachments/a')"
        )
    )
    conn.execute(
        text("INSERT INTO entry_attachments (entry_id, attachment_id, position) VALUES (5, 9, 0)")
    )
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(
            text(
                "INSERT INTO entry_attachments (entry_id, attachment_id, position) "
                "VALUES (5, 10, 1)"
            )
        )
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(
            text(
                "INSERT INTO entry_attachments (entry_id, attachment_id, position) VALUES (5, 9, 1)"
            )
        )


def test_entry_origin_round_trip() -> None:
    origin = EntryOrigin(12, datetime(2026, 11, 9, 17, 0, tzinfo=UTC), "ana")
    assert EntryOrigin.from_json(json.loads(json.dumps(origin.to_json()))) == origin
