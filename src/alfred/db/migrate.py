"""Run Alembic migrations from code (``alfred migrate`` and tests).

The migrations start at the baseline ``s001`` (alfred 0.17.0). A database
stamped with a revision this checkout doesn't know was created by alfred 0.16
or older, whose revisions were replaced; it is refused before anything runs.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, pool

_REPO_ROOT = Path(__file__).resolve().parents[3]  # src/alfred/db/migrate.py
_OLD_DATABASE = (
    "this database was created by alfred 0.16 or older and can't be upgraded; "
    "drop and recreate it (see the 0.17.0 handover update)"
)


class MigrationError(Exception):
    """A migration can't run as things are. The message says what to do."""


def upgrade(url: str, revision: str = "head") -> None:
    """Migrate the database at ``url`` (a SQLAlchemy URL) to ``revision``.

    Raises :class:`MigrationError`, having changed nothing, if the database
    is at a revision this checkout doesn't know.
    """
    cfg = _alembic_config(url)
    _refuse_unknown_revision(cfg, url)
    command.upgrade(cfg, revision)


def _alembic_config(url: str | None = None) -> Config:
    cfg = Config(str(_ini_path()))
    if url is not None:
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def _refuse_unknown_revision(cfg: Config, url: str) -> None:
    """Raise :class:`MigrationError` if ``alembic_version`` names an unknown revision.

    Only reads: a database with no ``alembic_version`` (a new one) passes.
    """
    known = {script.revision for script in ScriptDirectory.from_config(cfg).walk_revisions()}
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            stamped = MigrationContext.configure(conn).get_current_heads()
    finally:
        engine.dispose()
    if any(revision not in known for revision in stamped):
        raise MigrationError(_OLD_DATABASE)


def _ini_path() -> Path:
    # The image runs from /app, which holds alembic.ini and migrations/;
    # in a checkout, fall back to the repo root.
    for candidate in (Path.cwd() / "alembic.ini", _REPO_ROOT / "alembic.ini"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("alembic.ini not found in the working directory or the repo root")
