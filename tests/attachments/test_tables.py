"""Constraints of the attachments tables."""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

_INSERT = (
    "INSERT INTO attachments (user_name, kind, mime, size_bytes, object_key) "
    "VALUES ('d', :kind, 'image/jpeg', 1, :key)"
)


def test_attachment_kind_and_key_are_checked(conn: Connection) -> None:
    conn.execute(text(_INSERT), {"kind": "image", "key": "attachments/a"})
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(text(_INSERT), {"kind": "voice", "key": "attachments/b"})
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(text(_INSERT), {"kind": "pdf", "key": "attachments/a"})
