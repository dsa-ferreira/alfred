"""A user's current conversation: their one row of ``conversations`` with ``ended_at IS NULL``."""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from alfred.conversations.tables import Conversation


async def current_id(
    session: AsyncSession, user: str, *, lock: bool, exclusive: bool = False
) -> int:
    """``user``'s current conversation, created if they have none yet.

    ``exclusive`` takes it ``FOR UPDATE`` instead of ``FOR SHARE``, to archive
    it in the same transaction; two arrivals for the same user then take
    turns, so only one of them closes it.
    """
    query = select(Conversation.id).where(
        Conversation.ended_at.is_(None), Conversation.user_name == user
    )
    if lock:
        # A reset's UPDATE waits for this transaction, so the entry can't land
        # in a conversation that was archived before it committed.
        query = query.with_for_update(read=not exclusive)
    found = await session.scalar(query)
    if found is not None:
        return found
    await session.execute(
        insert(Conversation)
        .values(user_name=user)
        .on_conflict_do_nothing(index_elements=["user_name"], index_where=text("ended_at IS NULL"))
    )
    found = await session.scalar(query)
    assert found is not None
    return found
