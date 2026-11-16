"""Recording a message's files: their ``attachments`` rows and PDF pages."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from alfred.attachments import NewAttachment
from alfred.attachments.tables import Attachment, AttachmentPage


async def add(
    session: AsyncSession, user: str, files: Sequence[NewAttachment], *, queue_item_id: int
) -> list[int]:
    """Record ``user``'s ``files``, in message order, in the caller's transaction; their ids.

    The files must already be in the object store.
    """
    ids: list[int] = []
    for position, new in enumerate(files):
        row = Attachment(
            queue_item_id=queue_item_id,
            user_name=user,
            position=position,
            kind=new.kind,
            mime=new.mime,
            file_name=new.file_name,
            size_bytes=new.size_bytes,
            object_key=new.object_key,
            width=new.width,
            height=new.height,
            page_count=new.page_count,
        )
        session.add(row)
        await session.flush()
        session.add_all(
            AttachmentPage(attachment_id=row.id, page=n, text=page)
            for n, page in enumerate(new.pages, start=1)
        )
        ids.append(row.id)
    await session.flush()
    return ids
