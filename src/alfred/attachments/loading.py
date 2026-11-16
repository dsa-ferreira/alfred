"""Fetching an attachment from the store, ready for the model."""

from __future__ import annotations

from alfred.attachments.handles import AttachmentRef
from alfred.attachments.images import for_model_async
from alfred.attachments.pdf import render_page_async
from alfred.attachments.store import ObjectStore
from alfred.kernel.content import ModelImage


async def load_image(store: ObjectStore, ref: AttachmentRef, page: int, max_px: int) -> ModelImage:
    """Fetch the file and prepare it; raises :class:`StoreError` or :class:`Unreadable`."""
    data = await store.get(ref.object_key)
    if ref.kind == "pdf":
        return await render_page_async(data, page, max_px)
    return await for_model_async(data, max_px)
