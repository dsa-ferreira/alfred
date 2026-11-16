"""Startup checks that need more than the config file: alfred's tool names and
the user names the database knows.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable
from typing import ClassVar, Protocol

from sqlalchemy import select, union
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.db.models import Attachment, Conversation, QueueItem, Reminder, ReminderRecipient
from alfred.kernel.users import Users
from alfred.settings import ConfigError

log = logging.getLogger(__name__)


def check_tool_names(users: Users, toolset_types: Iterable[type[_Named]]) -> None:
    """Refuse exact ``alfred_*`` entries in users' tool and confirm lists that name no tool.

    ``toolset_types`` are alfred's own toolsets, as ``serve`` builds them.
    Patterns (``alfred_remind_*``) are left alone. Raises :class:`ConfigError`
    listing every such entry.
    """
    known = frozenset().union(*(t.names for t in toolset_types))
    problems = [
        f"users[{i}] ({user.name}): {key} entry {entry!r} is not an alfred tool"
        for i, user in enumerate(users)
        for key, allow in (("tools", user.tools), ("confirm", user.confirm))
        for entry in allow.exact("alfred_")
        if entry not in known
    ]
    if problems:
        raise ConfigError(problems)


async def warn_unknown_users(
    sessions: async_sessionmaker[AsyncSession], configured: Collection[str]
) -> list[str]:
    """Log a warning for each stored user name that isn't configured; returns them.

    Rows name their user by its ``[[users]]`` name. A name the config doesn't
    have (a typo in ``ALFRED_MIGRATE_ADMIN``, or a renamed user) leaves those
    rows invisible to every user. The fix is a manual ``UPDATE`` of the name.
    """
    unknown = sorted((await _stored_user_names(sessions)) - set(configured))
    for name in unknown:
        log.warning(
            "the database has conversations, messages or reminders of user %r, who is not "
            "in [[users]] (configured: %s); they are invisible until the name is fixed",
            name,
            ", ".join(configured),
        )
    return unknown


async def _stored_user_names(sessions: async_sessionmaker[AsyncSession]) -> set[str]:
    """Every user name some row refers to."""
    query = union(
        select(Conversation.user_name),
        select(QueueItem.user_name),
        select(Attachment.user_name),
        select(Reminder.created_by),
        select(ReminderRecipient.user_name),
    )
    async with sessions() as session:
        return set(await session.scalars(select(query.subquery().c[0])))


class _Named(Protocol):
    """A toolset class: every name it can ever offer."""

    names: ClassVar[frozenset[str]]
