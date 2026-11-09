"""``alfred_view_attachment`` and ``alfred_read_document`` (change add-attachments, design D8).

Offered on owner turns. ``alfred_read_document`` is offered on every brain;
``alfred_view_attachment`` only when the brain current at the turn's start
has vision, and a call checks the current brain again, which covers a
fallback within the turn.

Both reach only attachments sent in the current conversation or in a
conversation recalled into it. A recall only brings back the handles in its
transcript, so a conversation recalled inside a recalled one is reachable
only when it is itself recalled.

``alfred_read_document`` is not cut to ``[ai] tool_result_max_chars``: it
sizes its own result with ``[attachments] read_chars``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.brains import Brains
from alfred.attachments import Unreadable
from alfred.attachments.handles import AttachmentRef, handle, has_text, ref_query
from alfred.attachments.inline import load_image
from alfred.attachments.store import ObjectStore, StoreError
from alfred.config import AttachmentsConfig
from alfred.db.models import Attachment, AttachmentPage, Entry
from alfred.history import current_id
from alfred.tools.base import (
    AttemptContext,
    LocalToolset,
    PromptNote,
    ToolOutcome,
    TurnInput,
    owner_only,
    parse_arguments,
)

log = logging.getLogger(__name__)

VIEW_ATTACHMENT = "alfred_view_attachment"
READ_DOCUMENT = "alfred_read_document"

_ID = re.compile(r"#?\s*(\d+)")
_PAGES = re.compile(r"(\d+)(?:\s*-\s*(\d+))?")

VIEW_ATTACHMENT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": VIEW_ATTACHMENT,
        "description": (
            "Look at an attachment again: an image, or one page of a PDF rendered as an "
            "image. Use it when the owner asks about an attachment whose image is not in "
            "front of you, such as one from an earlier message, or a PDF page other than "
            "page 1."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "The attachment number, as in #7."},
                "page": {
                    "type": "integer",
                    "description": "For a PDF: the page to look at (default 1).",
                },
            },
            "required": ["id"],
        },
    },
}

READ_DOCUMENT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": READ_DOCUMENT,
        "description": (
            "Read the text of a PDF attachment, by pages. Each page comes marked with its "
            "number. A long range is cut, and the result says which page to read from next."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "The attachment number, as in #9."},
                "pages": {
                    "type": "string",
                    "description": 'One page ("4") or a range ("3-5"). Default: from page 1.',
                },
            },
            "required": ["id"],
        },
    },
}

_HANDLES = (
    "Attachments: the owner can send photos, images and PDFs. Each one appears in their "
    'message as a handle with its number, such as [attachment #7: image "receipt.jpg"] or '
    '[attachment #9: PDF "invoice.pdf", 12 pages, text] ("scanned" when page 1 has no '
    "text). Only attachments of this conversation, or of a conversation recalled into it, "
    "can be opened. Don't mention attachment numbers to the owner unless it helps them "
    "tell files apart."
)

VISION_NOTE = (
    _HANDLES + " On the message that brings it you also see the attachment: the image, or a PDF's "
    "page 1 (its text, or the page itself when it is scanned). A handle alone does not show "
    "the content: to answer about an attachment again, look at it with "
    "alfred_view_attachment (an image, or a PDF page as an image), or read a PDF's text with "
    "alfred_read_document. Never guess what an attachment shows."
)

TEXT_ONLY_NOTE = (
    _HANDLES
    + " This brain can't see images: when the owner asks about an image or a scanned page, "
    "say that you can't see it on this brain. A PDF's page 1 text comes with the message "
    "that brings it; a handle alone does not show the content, so read a PDF's text with "
    "alfred_read_document. Never guess what an attachment shows."
)


class AttachmentTools(LocalToolset):
    """The view and read tools, opened per turn with the current brain's vision."""

    def __init__(
        self,
        store: ObjectStore,
        sessions: async_sessionmaker[AsyncSession],
        brains: Brains,
        config: AttachmentsConfig,
    ) -> None:
        self._store = store
        self._sessions = sessions
        self._brains = brains
        self._config = config

    def offered(self, turn: TurnInput) -> bool:
        return owner_only(turn)

    async def open(self, turn: TurnInput) -> OpenAttachmentTools | None:
        if not self.offered(turn):
            return None
        return OpenAttachmentTools(self, vision=self._brains.current.vision)

    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
        arguments = parse_arguments(raw_arguments)
        if isinstance(arguments, str):
            return ToolOutcome(False, arguments)
        attachment_id = _parse_id(arguments.get("id"))
        if attachment_id is None:
            return ToolOutcome(False, "Error: id must be an attachment number, such as 7.")
        if name == VIEW_ATTACHMENT:
            return await self.view(attachment_id, arguments.get("page"))
        return await self.read(attachment_id, arguments.get("pages"))

    # --- view --------------------------------------------------------------------

    async def view(self, attachment_id: int, page_value: Any = None) -> ToolOutcome:
        if not self._brains.current.vision:
            return ToolOutcome(
                False,
                f'Error: the current brain "{self._brains.current.name}" can\'t see images.',
            )
        ref = await self.find(attachment_id)
        if ref is None:
            return _no_such(attachment_id)
        page = 1
        if ref.kind == "pdf":
            parsed = _parse_page(page_value)
            if parsed is None:
                return ToolOutcome(False, "Error: page must be a page number, such as 4.")
            page = parsed
            if page > (ref.page_count or 0):
                return ToolOutcome(False, _page_count(ref))
        try:
            image = await load_image(self._store, ref, page, self._config.image_max_px)
        except (StoreError, Unreadable, ValueError) as exc:
            log.warning("attachment #%s can't be viewed: %s", ref.id, exc)
            return ToolOutcome(
                False,
                f"Error: the file of attachment #{ref.id} isn't available right now. "
                "Tell the owner; it may work again later.",
            )
        shown = f"page {page} of {ref.page_count}" if ref.kind == "pdf" else "the image"
        return ToolOutcome(True, f"{handle(ref)}: {shown}, attached.", (image,))

    # --- read --------------------------------------------------------------------

    async def read(self, attachment_id: int, pages_value: Any = None) -> ToolOutcome:
        ref = await self.find(attachment_id)
        if ref is None:
            return _no_such(attachment_id)
        if ref.kind != "pdf":
            hint = " Look at it with alfred_view_attachment." if self._brains.current.vision else ""
            return ToolOutcome(False, f"Error: attachment #{ref.id} is an image, not a PDF.{hint}")
        count = ref.page_count or 0
        span = _parse_pages(pages_value, count)
        if span is None:
            return ToolOutcome(False, 'Error: pages must be one page ("4") or a range ("3-5").')
        first, last = span
        if first > count:
            return ToolOutcome(False, _page_count(ref))
        last = min(last, count)
        async with self._sessions() as session:
            rows = await session.execute(
                select(AttachmentPage.page, AttachmentPage.text)
                .where(
                    AttachmentPage.attachment_id == ref.id,
                    AttachmentPage.page.between(first, last),
                )
                .order_by(AttachmentPage.page)
            )
            texts = {page: text_ for page, text_ in rows}
        return ToolOutcome(True, self._render(ref, first, last, texts))

    def _render(self, ref: AttachmentRef, first: int, last: int, texts: dict[int, str]) -> str:
        """``--- page N ---`` blocks up to ``read_chars``, then where to continue."""
        limit = self._config.read_chars
        marker = (
            "(no text on this page; use alfred_view_attachment)"
            if self._brains.current.vision
            else "(no text on this page)"
        )
        blocks: list[str] = []
        used = 0
        for page in range(first, last + 1):
            text = (texts.get(page) or "").strip()
            if not has_text(text):
                text = f"{text}\n{marker}" if text else marker
            block = f"--- page {page} ---\n{text}"
            if used + len(block) <= limit:
                blocks.append(block)
                used += len(block)
                continue
            if not blocks:  # one page longer than the limit: its start, then the next page
                blocks.append(block[:limit] + f"\n(page {page} cut at {limit} characters)")
                page += 1
            if page <= (ref.page_count or 0):
                blocks.append(f"(continue from page {page})")
            break
        return "\n".join([handle(ref), *blocks])

    # --- access ------------------------------------------------------------------

    async def find(self, attachment_id: int) -> AttachmentRef | None:
        """The attachment, if the current conversation may reach it (design D8)."""
        async with self._sessions() as session:
            current = await current_id(session, lock=False)
            recalled = select(Entry.recalled_conversation_id).where(
                Entry.conversation_id == current, Entry.kind == "recall"
            )
            row = (
                await session.execute(
                    ref_query()
                    .join(Entry, Entry.id == Attachment.entry_id)
                    .where(
                        Attachment.id == attachment_id,
                        or_(
                            Entry.conversation_id == current,
                            Entry.conversation_id.in_(recalled.scalar_subquery()),
                        ),
                    )
                )
            ).first()
            await session.commit()  # current_id may have created the conversation
        return AttachmentRef(*row) if row is not None else None


class OpenAttachmentTools(LocalToolset):
    """One turn's attachment tools: the view tool only for a brain with vision."""

    def __init__(self, tools: AttachmentTools, *, vision: bool) -> None:
        self._tools = tools
        self.definitions = (
            [VIEW_ATTACHMENT_TOOL, READ_DOCUMENT_TOOL] if vision else [READ_DOCUMENT_TOOL]
        )
        self.prompt_notes = (PromptNote(VISION_NOTE if vision else TEXT_ONLY_NOTE),)

    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
        return await self._tools.call(name, raw_arguments, attempt)


def _no_such(attachment_id: int) -> ToolOutcome:
    return ToolOutcome(
        False, f"Error: there is no attachment #{attachment_id} in this conversation."
    )


def _page_count(ref: AttachmentRef) -> str:
    count = ref.page_count or 0
    return f"Error: attachment #{ref.id} is a PDF with {count} page{'' if count == 1 else 's'}."


def _parse_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        match = _ID.fullmatch(value.strip())
        if match and int(match.group(1)) > 0:
            return int(match.group(1))
    return None


def _parse_page(value: Any) -> int | None:
    if value is None or value == "":
        return 1
    return _parse_id(value)


def _parse_pages(value: Any, count: int) -> tuple[int, int] | None:
    """``(first, last)`` from ``"4"``, ``"3-5"`` or ``4``; no value is from page 1 on."""
    if value is None or value == "":
        return 1, max(count, 1)
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        return (value, value) if value > 0 else None
    if not isinstance(value, str):
        return None
    match = _PAGES.fullmatch(value.strip())
    if match is None:
        return None
    first = int(match.group(1))
    last = int(match.group(2)) if match.group(2) else first
    if first < 1 or last < first:
        return None
    return first, last
