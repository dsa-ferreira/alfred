"""Which users the database knows (change add-multiple-users, design D9).

Rows name their user by its ``[[users]]`` name. A name the config doesn't
have (a typo in ``ALFRED_MIGRATE_ADMIN``, or a renamed user) leaves those
rows invisible to every user, so ``serve`` warns about it at startup. The
fix is a manual ``UPDATE`` of the name.
"""

from __future__ import annotations

import logging
from collections.abc import Collection

from sqlalchemy import select, union
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.db.models import Attachment, Conversation, QueueItem, Reminder, ReminderRecipient

log = logging.getLogger(__name__)


async def warn_unknown_users(
    sessions: async_sessionmaker[AsyncSession], configured: Collection[str]
) -> list[str]:
    """Log a warning for each stored user name that isn't configured; returns them."""
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
