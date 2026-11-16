"""A user entry's files: linking them to the entry and reading their handles.

The files themselves belong to ``attachments``; ``entry_attachments`` says
which entry each was sent with, and in which order.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from alfred.features.attachments.handles import AttachmentRef, ref_query
from alfred.features.attachments.tables import Attachment
from alfred.features.conversations.tables import EntryAttachment


async def link(session: AsyncSession, entry_id: int, attachment_ids: Sequence[int]) -> None:
    """Record ``attachment_ids`` as the entry's files, in this order."""
    session.add_all(
        EntryAttachment(entry_id=entry_id, attachment_id=attachment_id, position=position)
        for position, attachment_id in enumerate(attachment_ids)
    )
    await session.flush()


async def refs_by_entry(
    session: AsyncSession, entry_ids: Iterable[int]
) -> dict[int, tuple[AttachmentRef, ...]]:
    """Each entry's attachments, in message order (one query)."""
    ids = list(entry_ids)
    if not ids:
        return {}
    rows = await session.execute(
        ref_query()
        .add_columns(EntryAttachment.entry_id)
        .join(EntryAttachment, EntryAttachment.attachment_id == Attachment.id)
        .where(EntryAttachment.entry_id.in_(ids))
        .order_by(EntryAttachment.entry_id, EntryAttachment.position)
    )
    found: dict[int, list[AttachmentRef]] = {}
    for *ref, entry_id in rows:
        found.setdefault(entry_id, []).append(AttachmentRef(*ref))
    return {entry_id: tuple(refs) for entry_id, refs in found.items()}
