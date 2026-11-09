"""Attachment handles: how the model and transcripts refer to a file (design D4).

``[attachment #7: image "receipt.jpg"]`` or
``[attachment #9: PDF "invoice.pdf", 12 pages, text]``. A PDF is ``text``
when page 1 has a text layer, else ``scanned``. Handles are built from
Postgres alone, so they never touch the object store.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from alfred.db.models import Attachment, AttachmentPage

# A page "has text" from this many characters on, so a stray page number on
# a scanned page doesn't count as a text layer.
MIN_TEXT_CHARS = 16


def has_text(text: str | None) -> bool:
    return text is not None and len(text.strip()) >= MIN_TEXT_CHARS


@dataclass(frozen=True)
class AttachmentRef:
    """What alfred knows about an attachment without fetching its file."""

    id: int
    kind: str  # image or pdf
    mime: str
    object_key: str
    file_name: str | None = None
    page_count: int | None = None
    first_page: str | None = None  # a PDF's page 1 text
    entry_id: int | None = None
    position: int = 0

    @property
    def first_page_has_text(self) -> bool:
        return has_text(self.first_page)


def handle(ref: AttachmentRef, note: str | None = None) -> str:
    """``[attachment #7: image "receipt.jpg"]``, with ``note`` in brackets before the end."""
    name = f' "{ref.file_name}"' if ref.file_name else ""
    if ref.kind == "pdf":
        pages = ref.page_count or 0
        layer = "text" if ref.first_page_has_text else "scanned"
        what = f"PDF{name}, {pages} page{'' if pages == 1 else 's'}, {layer}"
    else:
        what = f"image{name}"
    extra = f" ({note})" if note else ""
    return f"[attachment #{ref.id}: {what}{extra}]"


def with_handles(text: str, handles: Iterable[str]) -> str:
    """The message's text followed by its handles."""
    return " ".join([text, *handles]) if text else " ".join(handles)


def ref_columns() -> tuple:
    return (
        Attachment.id,
        Attachment.kind,
        Attachment.mime,
        Attachment.object_key,
        Attachment.file_name,
        Attachment.page_count,
        AttachmentPage.text,
        Attachment.entry_id,
        Attachment.position,
    )


def ref_query():  # noqa: ANN201 - a SQLAlchemy Select
    """Attachments with their page 1 text (PDFs), for :class:`AttachmentRef`."""
    return select(*ref_columns()).outerjoin(
        AttachmentPage,
        and_(AttachmentPage.attachment_id == Attachment.id, AttachmentPage.page == 1),
    )


async def refs_by_entry(
    session: AsyncSession, entry_ids: Iterable[int]
) -> dict[int, tuple[AttachmentRef, ...]]:
    """Each entry's attachments, in message order (one query)."""
    ids = list(entry_ids)
    if not ids:
        return {}
    rows = await session.execute(
        ref_query()
        .where(Attachment.entry_id.in_(ids))
        .order_by(Attachment.entry_id, Attachment.position)
    )
    found: dict[int, list[AttachmentRef]] = {}
    for row in rows:
        ref = AttachmentRef(*row)
        assert ref.entry_id is not None
        found.setdefault(ref.entry_id, []).append(ref)
    return {entry_id: tuple(refs) for entry_id, refs in found.items()}
