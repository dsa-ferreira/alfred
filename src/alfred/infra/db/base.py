"""The declarative base every slice's ``tables.py`` builds on.

Rows that belong to a user name them in ``user_name`` (``created_by`` for
reminders): a configured ``[[users]]`` name. There is no users table.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Deterministic constraint names, so migrations can refer to them.
_NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=_NAMING_CONVENTION)


def in_values(column: str, values: tuple[str, ...]) -> str:
    """A check constraint's SQL: ``column`` is one of ``values``."""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"
