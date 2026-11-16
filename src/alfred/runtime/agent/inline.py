"""Attachments sent with the message they arrived in.

The newest ``inline_turns`` user messages (default 1: the one being
answered) carry what they brought: an image as an image, a PDF as its
handle with up to ``first_page_chars`` of page 1's text, plus page 1 as an
image when it has no text. A brain without vision gets no images, and the
handle says so. Older messages keep only their handles.

Images are fetched and prepared once per turn (:class:`ImageCache`), so a
fallback attempt doesn't fetch them again. A file the store can't give gets
``(not available right now)`` on its handle, and the turn goes on.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from alfred.features.attachments import Unreadable
from alfred.features.attachments.config import AttachmentsConfig
from alfred.features.attachments.handles import AttachmentRef, handle
from alfred.features.attachments.loading import load_image
from alfred.features.attachments.store import ObjectStore, StoreError
from alfred.features.conversations.store import HistoryEntry
from alfred.kernel.content import ModelImage
from alfred.runtime.agent.messages import Inline

log = logging.getLogger(__name__)

_NO_VISION = "this brain can't see images"
_UNAVAILABLE = "not available right now"


class ImageCache:
    """One turn's prepared images, by attachment and page; ``None`` if unavailable."""

    def __init__(self, store: ObjectStore, max_px: int) -> None:
        self._store = store
        self._max_px = max_px
        self._images: dict[tuple[int, int], ModelImage | None] = {}

    async def image(self, ref: AttachmentRef, page: int = 1) -> ModelImage | None:
        """The image (or the PDF's ``page``) ready for the model, or ``None``."""
        key = (ref.id, page)
        if key not in self._images:
            self._images[key] = await self._load(ref, page)
        return self._images[key]

    async def _load(self, ref: AttachmentRef, page: int) -> ModelImage | None:
        try:
            return await load_image(self._store, ref, page, self._max_px)
        except (StoreError, Unreadable, ValueError) as exc:
            log.warning("attachment #%s is not available: %s", ref.id, exc)
            return None


class InlineAttachments:
    def __init__(self, store: ObjectStore, config: AttachmentsConfig) -> None:
        self._store = store
        self._config = config

    def new_cache(self) -> ImageCache:
        return ImageCache(self._store, self._config.image_max_px)

    async def build(
        self, entries: Sequence[HistoryEntry], *, vision: bool, cache: ImageCache
    ) -> dict[int, Inline]:
        """How each user entry in the inline window is sent, by entry ID."""
        users = [e for e in entries if e.kind == "user"]
        window = users[-self._config.inline_turns :] if self._config.inline_turns else []
        found: dict[int, Inline] = {}
        for entry in window:
            if entry.attachments and entry.entry_id is not None:
                found[entry.entry_id] = await self._entry(entry, vision, cache)
        return found

    async def _entry(self, entry: HistoryEntry, vision: bool, cache: ImageCache) -> Inline:
        inline = Inline([])
        for ref in entry.attachments:
            text_layer = ref.kind == "pdf" and ref.first_page_has_text
            note: str | None = None
            if not text_layer:  # an image, or a scanned page 1
                if not vision:
                    note = _NO_VISION
                else:
                    image = await cache.image(ref)
                    if image is None:
                        note = _UNAVAILABLE
                    else:
                        inline.images.append(image)
            shown = handle(ref, note)
            if text_layer:
                shown += "\n" + _first_page_block(ref, self._config.first_page_chars)
            inline.handles.append(shown)
        return inline


def _first_page_block(ref: AttachmentRef, max_chars: int) -> str:
    """A PDF's page 1 text as sent inline, cut to ``max_chars``."""
    text = (ref.first_page or "").strip()
    block = f"--- page 1 ---\n{text[:max_chars]}"
    if len(text) > max_chars:
        block += f"\n(page 1 cut at {max_chars} characters; read on with alfred_read_document)"
    return block
