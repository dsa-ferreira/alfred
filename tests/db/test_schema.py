from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


@contextlib.contextmanager
def _admin(name: str | None) -> Iterator[None]:
    """``ALFRED_MIGRATE_ADMIN`` set to ``name`` (unset for ``None``) inside the block."""
    with pytest.MonkeyPatch.context() as mp:
        if name is None:
            mp.delenv("ALFRED_MIGRATE_ADMIN", raising=False)
        else:
            mp.setenv("ALFRED_MIGRATE_ADMIN", name)
        yield


def test_migrate_creates_schema(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert {"conversations", "entries", "queue_items", "turn_tool_calls"} <= tables


def test_one_current_conversation_per_user(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            outer = conn.begin()
            conn.execute(text("TRUNCATE entries, conversations CASCADE"))
            conn.execute(text("INSERT INTO conversations (user_name) VALUES ('diogo')"))
            # An archived one is fine, and so is another user's current one...
            conn.execute(
                text("INSERT INTO conversations (user_name, ended_at) VALUES ('diogo', now())")
            )
            conn.execute(text("INSERT INTO conversations (user_name) VALUES ('ana')"))
            # ...a second current one for the same user is not.
            with pytest.raises(IntegrityError):
                conn.execute(text("INSERT INTO conversations (user_name) VALUES ('diogo')"))
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
                    "INSERT INTO conversations (user_name, ended_at, title, summary) "
                    "VALUES ('diogo', now(), 'Chicken thighs recipe', "
                    "'Lemon and garlic, 40 minutes.')"
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
            conn.execute(
                text("INSERT INTO conversations (id, user_name, ended_at) VALUES (41, 'd', now())")
            )
            conn.execute(text("INSERT INTO conversations (id, user_name) VALUES (42, 'd')"))
            conn.execute(
                text("INSERT INTO entries (conversation_id, kind, text) VALUES (41, 'user', 'hi')")
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

        with _admin("diogo"):
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
        "created_by",
        "created_at",
        "fired_at",
        "cancelled_at",
        "repeat",
        "repeat_days",
        "repeat_time",
        "quiet_ok",
    } == reminder_columns
    assert {"reminder_id", "reminder_due_at"} <= queue_columns
    assert {"reminder_id", "reminder_due_at"} <= entry_columns
    assert "ix_reminders_pending_due" in reminder_indexes


def test_reminder_status_is_checked(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            outer = conn.begin()
            with pytest.raises(IntegrityError):
                conn.execute(
                    text(
                        "INSERT INTO reminders (text, due_at, status, created_by) "
                        "VALUES ('x', now(), 'done', 'diogo')"
                    )
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
            conn.execute(text("INSERT INTO conversations (id, user_name) VALUES (42, 'd')"))
            conn.execute(
                text("INSERT INTO entries (conversation_id, kind, text) VALUES (42, 'user', 'hi')")
            )
            conn.execute(
                text(
                    "INSERT INTO reminders (text, due_at, created_by) "
                    "VALUES ('call mum', now(), 'd')"
                )
            )
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

        with _admin("diogo"):
            command.upgrade(cfg, "head")
        command.check(cfg)
    finally:
        engine.dispose()


# --- attachments (migration 0004) -------------------------------------------------


def test_attachment_schema(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        columns = {c["name"] for c in inspector.get_columns("attachments")}
        page_columns = {c["name"] for c in inspector.get_columns("attachment_pages")}
    finally:
        engine.dispose()
    assert {
        "id",
        "queue_item_id",
        "user_name",
        "entry_id",
        "position",
        "kind",
        "mime",
        "file_name",
        "size_bytes",
        "object_key",
        "width",
        "height",
        "page_count",
        "created_at",
    } == columns
    assert {"attachment_id", "page", "text"} == page_columns


def test_attachment_kind_and_key_are_checked(database_url: str) -> None:
    engine = create_engine(database_url)
    insert = (
        "INSERT INTO attachments "
        "(queue_item_id, user_name, position, kind, mime, size_bytes, object_key) "
        "VALUES (:item, 'd', 0, :kind, 'image/jpeg', 1, :key)"
    )
    try:
        with engine.connect() as conn:
            outer = conn.begin()
            item = conn.scalar(
                text(
                    "INSERT INTO queue_items (kind, user_name, text) "
                    "VALUES ('user', 'd', '') RETURNING id"
                )
            )
            conn.execute(text(insert), {"item": item, "kind": "image", "key": "attachments/a"})
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(text(insert), {"item": item, "kind": "voice", "key": "attachments/b"})
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(text(insert), {"item": item, "kind": "pdf", "key": "attachments/a"})
            outer.rollback()
    finally:
        engine.dispose()


def test_downgrade_to_0003(postgres_url: str) -> None:
    from alembic import command

    from alfred.db.migrate import alembic_config

    url = _new_database(postgres_url, "attachments_round_trip", vector=True)
    sa_url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    cfg = alembic_config(sa_url)
    command.upgrade(cfg, "head")

    engine = create_engine(sa_url)
    try:
        with engine.begin() as conn:
            item = conn.scalar(
                text(
                    "INSERT INTO queue_items (kind, user_name, text) "
                    "VALUES ('user', 'd', 'hi') RETURNING id"
                )
            )
            attachment = conn.scalar(
                text(
                    "INSERT INTO attachments (queue_item_id, user_name, position, kind, mime, "
                    "size_bytes, object_key, page_count) "
                    "VALUES (:item, 'd', 0, 'pdf', 'application/pdf', 10, 'attachments/x', 1) "
                    "RETURNING id"
                ),
                {"item": item},
            )
            conn.execute(
                text(
                    "INSERT INTO attachment_pages (attachment_id, page, text) VALUES (:a, 1, 'p')"
                ),
                {"a": attachment},
            )
        command.downgrade(cfg, "0003")
        tables = set(inspect(engine).get_table_names())
        assert "attachments" not in tables and "attachment_pages" not in tables
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT text FROM queue_items")) == "hi"

        with _admin("diogo"):
            command.upgrade(cfg, "head")
        command.check(cfg)
    finally:
        engine.dispose()


# --- users (migration 0005) -------------------------------------------------------


def _legacy_database(postgres_url: str, name: str) -> tuple[str, object]:
    """A database at 0004 holding one of everything a user owns. Returns its URL and config."""
    from alembic import command

    from alfred.db.migrate import alembic_config

    url = _new_database(postgres_url, name, vector=True)
    sa_url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    cfg = alembic_config(sa_url)
    command.upgrade(cfg, "0004")
    engine = create_engine(sa_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO conversations (id, ended_at) VALUES (41, now())"))
            conn.execute(text("INSERT INTO conversations (id) VALUES (42)"))
            conn.execute(
                text("INSERT INTO entries (conversation_id, kind, text) VALUES (42, 'owner', 'hi')")
            )
            conn.execute(text("INSERT INTO reminders (id, text, due_at) VALUES (12, 'x', now())"))
            conn.execute(text("INSERT INTO queue_items (id, kind, text) VALUES (7, 'owner', 'hi')"))
            conn.execute(
                text(
                    "INSERT INTO attachments "
                    "(queue_item_id, position, kind, mime, size_bytes, object_key) "
                    "VALUES (7, 0, 'image', 'image/jpeg', 1, 'attachments/a')"
                )
            )
    finally:
        engine.dispose()
    return sa_url, cfg


def test_users_migration_assigns_legacy_rows_to_the_admin(postgres_url: str) -> None:
    from alembic import command

    sa_url, cfg = _legacy_database(postgres_url, "users_legacy")
    with _admin("diogo"):
        command.upgrade(cfg, "head")
    engine = create_engine(sa_url)
    try:
        with engine.connect() as conn:
            for table in ("conversations", "queue_items", "attachments"):
                names = {n for (n,) in conn.execute(text(f"SELECT user_name FROM {table}"))}
                assert names == {"diogo"}, table
            assert conn.scalar(text("SELECT created_by FROM reminders")) == "diogo"
            assert list(conn.execute(text("SELECT * FROM reminder_recipients"))) == [(12, "diogo")]
            assert conn.scalar(text("SELECT kind FROM entries")) == "user"
            assert conn.scalar(text("SELECT kind FROM queue_items")) == "user"
            # Another user can now have a current conversation of their own.
            conn.execute(text("INSERT INTO conversations (user_name) VALUES ('ana')"))
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(text("INSERT INTO conversations (user_name) VALUES ('diogo')"))
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(text("INSERT INTO queue_items (kind, text) VALUES ('ask', 'x')"))
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(
                    text(
                        "INSERT INTO queue_items (kind, user_name, text) VALUES ('owner', 'd', 'x')"
                    )
                )
            conn.rollback()
        command.check(cfg)
    finally:
        engine.dispose()


def test_users_migration_of_an_empty_database_needs_no_admin(postgres_url: str) -> None:
    from alembic import command

    from alfred.db.migrate import alembic_config

    url = _new_database(postgres_url, "users_empty", vector=True)
    cfg = alembic_config(url.replace("postgresql://", "postgresql+psycopg://", 1))
    with _admin(None):
        command.upgrade(cfg, "head")
    command.check(cfg)


@pytest.mark.parametrize("value", [None, "", "  ", "Diogo", "diogo ferreira"])
def test_users_migration_refuses_rows_without_a_valid_admin(
    postgres_url: str,
    value: str | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from alfred.cli import main

    sa_url, _ = _legacy_database(postgres_url, "users_no_admin")
    monkeypatch.setenv("ALFRED_DATABASE_URL", sa_url.replace("+psycopg", ""))
    with _admin(value):
        assert main(["migrate"]) == 1
    err = capsys.readouterr().err
    assert "ALFRED_MIGRATE_ADMIN" in err and "[[users]] name" in err
    engine = create_engine(sa_url)
    try:
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0004"
            assert conn.scalar(text("SELECT kind FROM entries")) == "owner"
        tables = set(inspect(engine).get_table_names())
        assert "reminder_recipients" not in tables
        assert "user_name" not in {c["name"] for c in inspect(engine).get_columns("conversations")}
    finally:
        engine.dispose()


def test_users_downgrade_restores_0004(postgres_url: str) -> None:
    from alembic import command

    sa_url, cfg = _legacy_database(postgres_url, "users_round_trip")
    with _admin("diogo"):
        command.upgrade(cfg, "head")
    command.downgrade(cfg, "0004")
    engine = create_engine(sa_url)
    try:
        inspector = inspect(engine)
        assert "reminder_recipients" not in inspector.get_table_names()
        for table in ("conversations", "queue_items", "attachments"):
            assert "user_name" not in {c["name"] for c in inspector.get_columns(table)}, table
        assert "created_by" not in {c["name"] for c in inspector.get_columns("reminders")}
        assert "uq_conversations_one_current" in {
            i["name"] for i in inspector.get_indexes("conversations")
        }
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT kind FROM entries")) == "owner"
            assert conn.scalar(text("SELECT kind FROM queue_items")) == "owner"
            with pytest.raises(IntegrityError):
                conn.execute(text("INSERT INTO conversations DEFAULT VALUES"))
            conn.rollback()

        with _admin("diogo"):
            command.upgrade(cfg, "head")
        command.check(cfg)
    finally:
        engine.dispose()


# --- end reason (migration 0006) ----------------------------------------------------


def test_end_reason_upgrade_keeps_rows_and_is_checked(postgres_url: str) -> None:
    from alembic import command

    sa_url, cfg = _legacy_database(postgres_url, "end_reason")
    with _admin("diogo"):
        command.upgrade(cfg, "0005")
    command.upgrade(cfg, "head")
    engine = create_engine(sa_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, end_reason, marker_sent_at FROM conversations ORDER BY id")
            ).all()
            assert rows == [(41, None, None), (42, None, None)]
            conn.execute(text("UPDATE conversations SET end_reason = 'idle' WHERE id = 41"))
            conn.execute(text("UPDATE conversations SET end_reason = 'user' WHERE id = 41"))
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(text("UPDATE conversations SET end_reason = 'timeout' WHERE id = 41"))
            conn.rollback()
        command.check(cfg)

        command.downgrade(cfg, "0005")
        columns = {c["name"] for c in inspect(engine).get_columns("conversations")}
        assert not {"end_reason", "marker_sent_at"} & columns
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM conversations")) == 2
        command.upgrade(cfg, "head")
        command.check(cfg)
    finally:
        engine.dispose()


# --- user facts (migration 0007) ------------------------------------------------------


def test_facts_upgrade_leaves_archived_conversations_pending(postgres_url: str) -> None:
    from alembic import command

    sa_url, cfg = _legacy_database(postgres_url, "user_facts")
    with _admin("diogo"):
        command.upgrade(cfg, "0006")
    command.upgrade(cfg, "head")
    engine = create_engine(sa_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, facts_outcome, facts_attempts, facts_next_at "
                    "FROM conversations ORDER BY id"
                )
            ).all()
            assert rows == [(41, None, 0, None), (42, None, 0, None)]
        command.check(cfg)

        command.downgrade(cfg, "0006")
        assert "facts" not in inspect(engine).get_table_names()
        columns = {c["name"] for c in inspect(engine).get_columns("conversations")}
        assert not {"facts_outcome", "facts_attempts", "facts_next_at"} & columns
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM conversations")) == 2
        command.upgrade(cfg, "head")
        command.check(cfg)
    finally:
        engine.dispose()


def test_fact_checks(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            conn.execute(text("TRUNCATE facts, entries, conversations CASCADE"))
            conn.execute(
                text("INSERT INTO facts (user_name, text, origin) VALUES ('d', 'x', 'told')")
            )
            conn.execute(
                text("INSERT INTO facts (user_name, text, origin) VALUES ('d', 'x', 'learned')")
            )
            bad = [
                "INSERT INTO facts (user_name, text, origin) VALUES ('d', 'x', 'guessed')",
                "INSERT INTO facts (user_name, text, origin, removed_at, removed_reason) "
                "VALUES ('d', 'x', 'told', now(), 'lost')",
                "INSERT INTO facts (user_name, text, origin, removed_at) "
                "VALUES ('d', 'x', 'told', now())",
                "INSERT INTO facts (user_name, text, origin, removed_reason) "
                "VALUES ('d', 'x', 'told', 'forgotten')",
                "INSERT INTO conversations (user_name, ended_at, facts_outcome) "
                "VALUES ('d', now(), 'skipped')",
            ]
            for statement in bad:
                with pytest.raises(IntegrityError), conn.begin_nested():
                    conn.execute(text(statement))
            for reason in ("forgotten", "replaced", "duplicate"):
                conn.execute(
                    text(
                        "INSERT INTO facts (user_name, text, origin, removed_at, removed_reason) "
                        f"VALUES ('d', 'x', 'told', now(), '{reason}')"
                    )
                )
            for outcome in ("done", "gave_up"):
                conn.execute(
                    text(
                        "INSERT INTO conversations (user_name, ended_at, facts_outcome) "
                        f"VALUES ('d', now(), '{outcome}')"
                    )
                )
            conn.rollback()
    finally:
        engine.dispose()


def test_deleting_a_conversation_keeps_its_facts(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            conn.execute(text("TRUNCATE facts, entries, conversations CASCADE"))
            conversation = conn.scalar(
                text(
                    "INSERT INTO conversations (user_name, ended_at) "
                    "VALUES ('d', now()) RETURNING id"
                )
            )
            fact = conn.scalar(
                text(
                    "INSERT INTO facts (user_name, text, origin, conversation_id) "
                    "VALUES ('d', 'x', 'learned', :c) RETURNING id"
                ),
                {"c": conversation},
            )
            conn.execute(text("DELETE FROM conversations WHERE id = :c"), {"c": conversation})
            assert (
                conn.scalar(text("SELECT conversation_id FROM facts WHERE id = :f"), {"f": fact})
                is None
            )
            conn.rollback()
    finally:
        engine.dispose()


# --- recurring reminders (migration 0008) ----------------------------------------------


def test_recurring_upgrade_backfills_due_times(postgres_url: str) -> None:
    from alembic import command

    sa_url, cfg = _legacy_database(postgres_url, "recurring")
    with _admin("diogo"):
        command.upgrade(cfg, "0007")
    engine = create_engine(sa_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE reminders SET due_at = '2026-11-16 08:00+00' WHERE id = 12"))
            conn.execute(
                text(
                    "INSERT INTO entries (conversation_id, kind, text, reminder_id) "
                    "VALUES (42, 'reminder', 'x', 12)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO queue_items (id, kind, user_name, text, reminder_id) "
                    "VALUES (8, 'reminder', 'diogo', 'x', 12)"
                )
            )
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            due = conn.scalar(text("SELECT due_at FROM reminders WHERE id = 12"))
            assert (
                conn.scalar(text("SELECT reminder_due_at FROM entries WHERE kind = 'reminder'"))
                == due
            )
            assert conn.scalar(text("SELECT reminder_due_at FROM queue_items WHERE id = 8")) == due
            # Rows that aren't reminders have none.
            assert conn.scalar(text("SELECT reminder_due_at FROM queue_items WHERE id = 7")) is None
            assert conn.execute(
                text("SELECT repeat, repeat_days, repeat_time, quiet_ok FROM reminders")
            ).all() == [(None, None, None, False)]
        command.check(cfg)
    finally:
        engine.dispose()


def test_repeat_rule_checks(database_url: str) -> None:
    engine = create_engine(database_url)
    insert = (
        "INSERT INTO reminders (text, due_at, created_by, repeat, repeat_days, repeat_time) "
        "VALUES ('x', now(), 'd', {})"
    )
    try:
        with engine.connect() as conn:
            for rule in ("'daily', 127, '08:00'", "'weekly', 1, '20:00'", "NULL, NULL, NULL"):
                conn.execute(text(insert.format(rule)))
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
                    conn.execute(text(insert.format(rule)))
            conn.execute(
                text(
                    "INSERT INTO conversations (user_name, ended_at, end_reason) "
                    "VALUES ('d', now(), 'unattended')"
                )
            )
            conn.rollback()
    finally:
        engine.dispose()


def test_recurring_downgrade(postgres_url: str) -> None:
    from alembic import command

    sa_url, cfg = _legacy_database(postgres_url, "recurring_down")
    with _admin("diogo"):
        command.upgrade(cfg, "head")
    engine = create_engine(sa_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO reminders "
                    "(id, text, due_at, created_by, repeat, repeat_days, repeat_time) "
                    "VALUES (31, 'brief me', now(), 'diogo', 'weekdays', 31, '08:00')"
                )
            )
            conn.execute(text("UPDATE conversations SET end_reason = 'unattended' WHERE id = 41"))
        command.downgrade(cfg, "0007")
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT id, status, cancelled_at IS NOT NULL FROM reminders ORDER BY id")
            ).all() == [(12, "pending", False), (31, "cancelled", True)]
            assert conn.execute(
                text(
                    "SELECT end_reason, marker_sent_at IS NOT NULL FROM conversations WHERE id = 41"
                )
            ).one() == ("idle", True)
        columns = {c["name"] for c in inspect(engine).get_columns("reminders")}
        assert not {"repeat", "repeat_days", "repeat_time", "quiet_ok"} & columns
        command.upgrade(cfg, "head")
        command.check(cfg)
    finally:
        engine.dispose()


# --- fact duplicate check (migration 0009) ----------------------------------------------


def test_duplicate_check_upgrade_marks_existing_facts_checked(postgres_url: str) -> None:
    from alembic import command

    sa_url, cfg = _legacy_database(postgres_url, "fact_duplicates")
    with _admin("diogo"):
        command.upgrade(cfg, "0008")
    engine = create_engine(sa_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO facts (id, user_name, text, origin) VALUES "
                    "(1, 'diogo', 'active', 'told'), (2, 'diogo', 'gone', 'learned')"
                )
            )
            conn.execute(
                text(
                    "UPDATE facts SET removed_at = now(), removed_reason = 'duplicate', "
                    "replaced_by = 1 WHERE id = 2"
                )
            )
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, duplicate_checked_at IS NOT NULL, duplicate_attempts, "
                    "duplicate_next_at FROM facts ORDER BY id"
                )
            ).all()
            assert rows == [(1, True, 0, None), (2, True, 0, None)]
            conn.execute(
                text(
                    "INSERT INTO facts (id, user_name, text, origin) "
                    "VALUES (3, 'diogo', 'n', 'told')"
                )
            )
            assert conn.scalar(text("SELECT duplicate_checked_at FROM facts WHERE id = 3")) is None
        command.check(cfg)

        command.downgrade(cfg, "0008")
        columns = {c["name"] for c in inspect(engine).get_columns("facts")}
        assert not {"duplicate_checked_at", "duplicate_attempts", "duplicate_next_at"} & columns
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM facts")) == 3
        command.upgrade(cfg, "head")
        command.check(cfg)
    finally:
        engine.dispose()
