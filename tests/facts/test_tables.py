"""Constraints of the facts tables."""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError


def test_fact_checks(conn: Connection) -> None:
    conn.execute(text("INSERT INTO facts (user_name, text, origin) VALUES ('d', 'x', 'told')"))
    conn.execute(text("INSERT INTO facts (user_name, text, origin) VALUES ('d', 'x', 'learned')"))
    bad = [
        "INSERT INTO facts (user_name, text, origin) VALUES ('d', 'x', 'guessed')",
        "INSERT INTO facts (user_name, text, origin, removed_at, removed_reason) "
        "VALUES ('d', 'x', 'told', now(), 'lost')",
        "INSERT INTO facts (user_name, text, origin, removed_at) VALUES ('d', 'x', 'told', now())",
        "INSERT INTO facts (user_name, text, origin, removed_reason) "
        "VALUES ('d', 'x', 'told', 'forgotten')",
    ]
    for statement in bad:
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(text(statement))
    for reason in ("forgotten", "replaced", "duplicate"):
        conn.execute(
            text(
                "INSERT INTO facts (user_name, text, origin, removed_at, removed_reason) "
                "VALUES ('d', 'x', 'told', now(), :reason)"
            ),
            {"reason": reason},
        )


def test_extraction_outcome_is_checked(conn: Connection) -> None:
    for cid, outcome in ((1, "done"), (2, "gave_up"), (3, None)):
        conn.execute(
            text("INSERT INTO conversations (id, user_name, ended_at) VALUES (:c, 'd', now())"),
            {"c": cid},
        )
        conn.execute(
            text("INSERT INTO fact_extractions (conversation_id, outcome) VALUES (:c, :o)"),
            {"c": cid, "o": outcome},
        )
    conn.execute(text("INSERT INTO conversations (id, user_name, ended_at) VALUES (4, 'd', now())"))
    with pytest.raises(IntegrityError):
        conn.execute(
            text("INSERT INTO fact_extractions (conversation_id, outcome) VALUES (4, 'skipped')")
        )


def test_deleting_a_conversation_keeps_its_facts(conn: Connection) -> None:
    conversation = conn.scalar(
        text("INSERT INTO conversations (user_name, ended_at) VALUES ('d', now()) RETURNING id")
    )
    fact = conn.scalar(
        text(
            "INSERT INTO facts (user_name, text, origin, conversation_id) "
            "VALUES ('d', 'x', 'learned', :c) RETURNING id"
        ),
        {"c": conversation},
    )
    conn.execute(text("DELETE FROM conversations WHERE id = :c"), {"c": conversation})
    assert conn.scalar(text("SELECT conversation_id FROM facts WHERE id = :f"), {"f": fact}) is None
