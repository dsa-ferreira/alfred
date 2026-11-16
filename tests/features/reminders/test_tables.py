"""Constraints of the reminders tables."""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

_INSERT = (
    "INSERT INTO reminders (text, due_at, created_by, repeat, repeat_days, repeat_time) "
    "VALUES ('x', now(), 'd', {})"
)


def test_reminder_status_is_checked(conn: Connection) -> None:
    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO reminders (text, due_at, status, created_by) "
                "VALUES ('x', now(), 'done', 'diogo')"
            )
        )


def test_repeat_rule_checks(conn: Connection) -> None:
    for rule in ("'daily', 127, '08:00'", "'weekly', 1, '20:00'", "NULL, NULL, NULL"):
        conn.execute(text(_INSERT.format(rule)))
    bad = [
        "'monthly', 127, '08:00'",
        "'daily', 127, NULL",
        "'daily', NULL, '08:00'",
        "NULL, 31, NULL",
        "'weekly', 0, '08:00'",
        "'weekly', 128, '08:00'",
    ]
    for rule in bad:
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(text(_INSERT.format(rule)))
