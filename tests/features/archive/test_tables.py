"""Constraints and generated columns of the archive's table."""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError


def test_search_tsv_is_generated(conn: Connection) -> None:
    conn.execute(
        text("INSERT INTO conversations (id, user_name, ended_at) VALUES (41, 'diogo', now())")
    )
    conn.execute(
        text(
            "INSERT INTO conversation_summaries (conversation_id, title, summary) "
            "VALUES (41, 'Chicken thighs recipe', 'Lemon and garlic, 40 minutes.')"
        )
    )
    found = conn.scalar(
        text(
            "SELECT count(*) FROM conversation_summaries "
            "WHERE search_tsv @@ websearch_to_tsquery('english', 'garlic chickens')"
        )
    )
    assert found == 1


def test_a_summary_needs_its_conversation(conn: Connection) -> None:
    with pytest.raises(IntegrityError):
        conn.execute(text("INSERT INTO conversation_summaries (conversation_id) VALUES (99)"))
