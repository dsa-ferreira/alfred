"""Constraints of the turns tables."""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

_REQUEST = (
    "INSERT INTO input_requests "
    "(queue_item_id, user_name, status, tool, arguments, question, state, turn, expires_at) "
    "VALUES (1, 'ana', '{}', 't', '{{}}', '{{}}', '{{}}', '{{}}', now())"
)
_ITEM = "INSERT INTO queue_items (id, kind, user_name, payload) VALUES (:id, :kind, 'ana', '{}')"


def test_queue_kind_is_checked(conn: Connection) -> None:
    for n, kind in enumerate(("chat", "ask", "reminder"), start=1):
        conn.execute(text(_ITEM), {"id": n, "kind": kind})
    # "user" was a chat message's stored kind before 0.17.0.
    with pytest.raises(IntegrityError):
        conn.execute(text(_ITEM), {"id": 9, "kind": "user"})


def test_input_request_checks(conn: Connection) -> None:
    conn.execute(text(_ITEM), {"id": 1, "kind": "chat"})
    for status in ("waiting", "cancelled", "pending"):
        conn.execute(text(f"UPDATE queue_items SET status = '{status}' WHERE id = 1"))
    conn.execute(text(_REQUEST.format("open")))
    # One open and one done for the same item are fine...
    conn.execute(text(_REQUEST.format("done")))
    # ...a second live one is not.
    for live in ("open", "answered", "running"):
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(text(_REQUEST.format(live)))
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(text(_REQUEST.format("maybe")))
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(text("UPDATE queue_items SET status = 'parked' WHERE id = 1"))
