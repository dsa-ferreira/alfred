from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def test_migrate_creates_schema(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert {"conversations", "entries", "queue_items", "turn_tool_calls"} <= tables


def test_only_one_current_conversation(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            outer = conn.begin()
            conn.execute(text("TRUNCATE entries, conversations CASCADE"))
            conn.execute(text("INSERT INTO conversations DEFAULT VALUES"))
            # An archived one is fine...
            conn.execute(text("INSERT INTO conversations (ended_at) VALUES (now())"))
            # ...a second current one is not.
            with pytest.raises(IntegrityError):
                conn.execute(text("INSERT INTO conversations DEFAULT VALUES"))
            outer.rollback()
    finally:
        engine.dispose()


def test_models_match_migrations(database_url: str) -> None:
    from alembic import command

    from alfred.db.migrate import alembic_config

    command.check(alembic_config(database_url))
