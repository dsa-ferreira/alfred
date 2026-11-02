"""Run Alembic migrations from code (``alfred migrate`` and tests)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

_REPO_ROOT = Path(__file__).resolve().parents[3]  # src/alfred/db/migrate.py


class MigrationError(Exception):
    """A migration can't run as things are. The message says what to do."""


def alembic_config(url: str | None = None) -> Config:
    cfg = Config(str(_ini_path()))
    if url is not None:
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade(url: str | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def _ini_path() -> Path:
    # The image runs from /app, which holds alembic.ini and migrations/;
    # in a checkout, fall back to the repo root.
    for candidate in (Path.cwd() / "alembic.ini", _REPO_ROOT / "alembic.ini"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("alembic.ini not found in the working directory or the repo root")
