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


# --- conversation memory (migration 0002) ---------------------------------------


def _new_database(postgres_url: str, name: str, *, vector: bool) -> str:
    """Create an empty database in the test container; returns its libpq URL."""
    import psycopg

    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name}")
        conn.execute(f"CREATE DATABASE {name}")
    url = postgres_url.rsplit("/", 1)[0] + f"/{name}"
    if vector:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION vector")
    return url


def test_memory_columns_exist(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        conversation_columns = {c["name"] for c in inspector.get_columns("conversations")}
        entry_columns = {c["name"] for c in inspector.get_columns("entries")}
        entry_indexes = {i["name"] for i in inspector.get_indexes("entries")}
    finally:
        engine.dispose()
    assert {
        "title",
        "summary",
        "summarised_at",
        "embedding",
        "embedding_model",
        "summary_attempts",
        "summary_next_at",
        "search_tsv",
    } <= conversation_columns
    assert "recalled_conversation_id" in entry_columns
    assert {"ix_entries_text_tsv", "uq_entries_recall"} <= entry_indexes


def test_search_tsv_is_generated(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            outer = conn.begin()
            conn.execute(text("TRUNCATE entries, conversations CASCADE"))
            conn.execute(
                text(
                    "INSERT INTO conversations (ended_at, title, summary) "
                    "VALUES (now(), 'Chicken thighs recipe', 'Lemon and garlic, 40 minutes.')"
                )
            )
            found = conn.scalar(
                text(
                    "SELECT count(*) FROM conversations "
                    "WHERE search_tsv @@ websearch_to_tsquery('english', 'garlic chickens')"
                )
            )
            outer.rollback()
    finally:
        engine.dispose()
    assert found == 1


def test_recall_not_twice_into_one_conversation(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            outer = conn.begin()
            conn.execute(text("TRUNCATE entries, conversations CASCADE"))
            conn.execute(text("INSERT INTO conversations (id, ended_at) VALUES (41, now())"))
            conn.execute(text("INSERT INTO conversations (id) VALUES (42)"))
            insert = text(
                "INSERT INTO entries (conversation_id, kind, text, recalled_conversation_id) "
                "VALUES (42, 'recall', 'x', 41)"
            )
            conn.execute(insert)
            with pytest.raises(IntegrityError):
                conn.execute(insert)
            outer.rollback()
    finally:
        engine.dispose()


def test_migration_fails_clearly_without_vector(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from alfred.cli import main

    url = _new_database(postgres_url, "no_vector", vector=False)
    monkeypatch.setenv("ALFRED_DATABASE_URL", url)
    assert main(["migrate"]) == 1
    err = capsys.readouterr().err
    assert "'vector' extension" in err
    assert "CREATE EXTENSION IF NOT EXISTS vector" in err
    assert "superuser" in err


def test_upgrade_downgrade_upgrade(postgres_url: str) -> None:
    from alembic import command

    from alfred.db.migrate import alembic_config

    url = _new_database(postgres_url, "round_trip", vector=True)
    sa_url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    cfg = alembic_config(sa_url)
    command.upgrade(cfg, "head")

    engine = create_engine(sa_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO conversations (id, ended_at) VALUES (41, now())"))
            conn.execute(text("INSERT INTO conversations (id) VALUES (42)"))
            conn.execute(
                text("INSERT INTO entries (conversation_id, kind, text) VALUES (41, 'owner', 'hi')")
            )
            conn.execute(
                text(
                    "INSERT INTO entries (conversation_id, kind, text, recalled_conversation_id) "
                    "VALUES (42, 'recall', 'x', 41)"
                )
            )
        command.downgrade(cfg, "0001")
        inspector = inspect(engine)
        assert "title" not in {c["name"] for c in inspector.get_columns("conversations")}
        with engine.connect() as conn:
            kinds = [k for (k,) in conn.execute(text("SELECT kind FROM entries"))]
        assert kinds == ["owner"]  # the recall entry is gone, the rest is kept

        command.upgrade(cfg, "head")
        command.check(cfg)
    finally:
        engine.dispose()


# --- reminders (migration 0003) ---------------------------------------------------


def test_reminder_schema(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        reminder_columns = {c["name"] for c in inspector.get_columns("reminders")}
        queue_columns = {c["name"] for c in inspector.get_columns("queue_items")}
        entry_columns = {c["name"] for c in inspector.get_columns("entries")}
        reminder_indexes = {i["name"] for i in inspector.get_indexes("reminders")}
    finally:
        engine.dispose()
    assert {
        "id",
        "text",
        "due_at",
        "status",
        "created_at",
        "fired_at",
        "cancelled_at",
    } == reminder_columns
    assert "reminder_id" in queue_columns
    assert "reminder_id" in entry_columns
    assert "ix_reminders_pending_due" in reminder_indexes


def test_reminder_status_is_checked(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            outer = conn.begin()
            with pytest.raises(IntegrityError):
                conn.execute(
                    text("INSERT INTO reminders (text, due_at, status) VALUES ('x', now(), 'done')")
                )
            outer.rollback()
    finally:
        engine.dispose()


def test_downgrade_to_0002(postgres_url: str) -> None:
    from alembic import command

    from alfred.db.migrate import alembic_config

    url = _new_database(postgres_url, "reminders_round_trip", vector=True)
    sa_url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    cfg = alembic_config(sa_url)
    command.upgrade(cfg, "head")

    engine = create_engine(sa_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO conversations (id) VALUES (42)"))
            conn.execute(
                text("INSERT INTO entries (conversation_id, kind, text) VALUES (42, 'owner', 'hi')")
            )
            conn.execute(text("INSERT INTO reminders (text, due_at) VALUES ('call mum', now())"))
        command.downgrade(cfg, "0002")
        inspector = inspect(engine)
        assert "reminders" not in inspector.get_table_names()
        assert "reminder_id" not in {c["name"] for c in inspector.get_columns("entries")}
        assert "reminder_id" not in {c["name"] for c in inspector.get_columns("queue_items")}
        with engine.connect() as conn:
            outer = conn.begin()
            with pytest.raises(IntegrityError):
                conn.execute(text("INSERT INTO queue_items (kind, text) VALUES ('reminder', 'x')"))
            outer.rollback()
            kinds = [k for (k,) in conn.execute(text("SELECT kind FROM entries"))]
        assert kinds == ["owner"]

        command.upgrade(cfg, "head")
        command.check(cfg)
    finally:
        engine.dispose()
