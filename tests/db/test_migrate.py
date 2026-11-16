"""Migrating: the baseline, the vector check, and the refusal of old databases (design D6)."""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from alfred.db.migrate import MigrationError, _alembic_config, upgrade
from alfred.schema import metadata


def _new_database(postgres_url: str, name: str, *, vector: bool) -> str:
    """Create an empty database in the test container; returns its SQLAlchemy URL."""
    import psycopg

    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name}")
        conn.execute(f"CREATE DATABASE {name}")
    url = postgres_url.rsplit("/", 1)[0] + f"/{name}"
    if vector:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION vector")
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


def test_migrate_creates_every_table(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert set(metadata.tables) <= tables


def test_models_match_migrations(database_url: str) -> None:
    command.check(_alembic_config(database_url))


def test_migration_fails_clearly_without_vector(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from alfred.cli import _main

    url = _new_database(postgres_url, "no_vector", vector=False)
    monkeypatch.setenv("ALFRED_DATABASE_URL", url)
    assert _main(["migrate"]) == 1
    err = capsys.readouterr().err
    assert "'vector' extension" in err
    assert "CREATE EXTENSION IF NOT EXISTS vector" in err
    assert "superuser" in err


def test_baseline_upgrade_downgrade_upgrade(postgres_url: str) -> None:
    url = _new_database(postgres_url, "round_trip", vector=True)
    cfg = _alembic_config(url)
    upgrade(url)
    engine = create_engine(url)
    try:
        command.downgrade(cfg, "base")
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
        upgrade(url)
        command.check(cfg)
        assert set(metadata.tables) <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_an_old_database_is_refused_untouched(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from alfred.cli import _main

    url = _new_database(postgres_url, "from_0_16", vector=True)
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)"))
            conn.execute(text("INSERT INTO alembic_version VALUES ('0010')"))
            conn.execute(text("CREATE TABLE conversations (id bigint PRIMARY KEY)"))
        with pytest.raises(MigrationError, match="alfred 0.16 or older") as raised:
            upgrade(url)
        assert "drop and recreate it" in str(raised.value)
        assert "0.17.0 handover" in str(raised.value)
        with engine.connect() as conn:
            assert list(conn.scalars(text("SELECT version_num FROM alembic_version"))) == ["0010"]
        assert set(inspect(engine).get_table_names()) == {"alembic_version", "conversations"}

        monkeypatch.setenv("ALFRED_DATABASE_URL", url)
        assert _main(["migrate"]) == 1
        assert "alfred 0.16 or older" in capsys.readouterr().err
    finally:
        engine.dispose()
