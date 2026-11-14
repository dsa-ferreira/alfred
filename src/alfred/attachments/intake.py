"""Receiving a user's files: check, download, read, store, queue (design D2).

```
each file:  size from Telegram > max_bytes -> too_large, skip (not downloaded)
            download                       -> failure: not_kept, queue nothing
            decode the image / read the PDF -> unreadable, skip
any left:   PUT each to attachments/<uuid4> -> failure: not_kept, queue nothing
            one transaction: queue item + attachments + PDF pages
```

A refused file gets its own notice and the rest of the message (an album)
is still queued; if every file was refused, nothing is. A crash after a PUT
and before the commit leaves an orphan object, which the sweeper deletes.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from alfred import notices
from alfred.attachments import AttachmentKind, NewAttachment, Unreadable
from alfred.attachments.images import inspect_image_async
from alfred.attachments.pdf import MIME as PDF_MIME
from alfred.attachments.pdf import read_pdf_async
from alfred.attachments.store import PREFIX, ObjectStore, StoreError
from alfred.config import AttachmentsConfig, User
from alfred.queue import Queue

log = logging.getLogger(__name__)


class DownloadFailed(Exception):
    """Telegram didn't hand over the file."""


class Downloader(Protocol):
    async def download(self, file_id: str) -> bytes:
        """The file's bytes; raises :class:`DownloadFailed`."""
        ...


@dataclass(frozen=True)
class IncomingFile:
    """One file of a user's message, as Telegram describes it, before download."""

    kind: AttachmentKind
    file_id: str
    size: int | None = None  # Telegram's, when it gives one
    file_name: str | None = None


@dataclass
class Received:
    """What became of a message: the queue item (if any) and the notices to send."""

    item_id: int | None = None
    notices: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Ready:
    attachment: NewAttachment
    data: bytes


class Intake:
    def __init__(
        self,
        config: AttachmentsConfig,
        store: ObjectStore,
        queue: Queue,
        downloader: Downloader,
    ) -> None:
        self._config = config
        self._store = store
        self._queue = queue
        self._downloader = downloader

    async def receive(
        self,
        user: User,
        text: str,
        files: Sequence[IncomingFile],
        telegram_message_id: int | None = None,
    ) -> Received:
        """Queue ``text`` with the usable ``files``, in order, as ``user``'s message.

        The notices are in ``user``'s form of address.
        """
        address = user.address
        result = Received()
        ready: list[_Ready] = []
        for file in files:
            try:
                found = await self._take(file, address)
            except DownloadFailed as exc:
                log.warning("message %s: download failed: %s", telegram_message_id, exc)
                return Received(notices=[notices.not_kept(address)])
            if isinstance(found, str):
                result.notices.append(found)
            else:
                ready.append(found)
        if not ready:
            return result

        try:
            for item in ready:
                await self._store.put(item.attachment.object_key, item.data, item.attachment.mime)
        except StoreError:
            # Already logged, without secrets. Objects put so far are orphans.
            return Received(notices=[notices.not_kept(address)])

        attachments = [r.attachment for r in ready]
        result.item_id = await self._queue.enqueue_user(
            user.name, text, telegram_message_id, attachments
        )
        log.info(
            "queued %s's message %s as item %s with %s",
            user.name,
            telegram_message_id,
            result.item_id,
            ", ".join(f"{a.kind} {a.size_bytes} bytes" for a in attachments),
        )
        return result

    async def _take(self, file: IncomingFile, address: str) -> _Ready | str:
        """The file downloaded and read, or the notice that refuses it."""
        limit = self._config.max_bytes
        if file.size is not None and file.size > limit:
            log.info("refused a %s of %d bytes: over the limit", file.kind, file.size)
            return notices.too_large(address, limit)
        data = await self._downloader.download(file.file_id)
        if len(data) > limit:
            log.info("refused a %s of %d bytes: over the limit", file.kind, len(data))
            return notices.too_large(address, limit)
        key = f"{PREFIX}{uuid.uuid4()}"
        try:
            if file.kind == "image":
                info = await inspect_image_async(data)
                new = NewAttachment(
                    "image",
                    info.mime,
                    len(data),
                    key,
                    file.file_name,
                    width=info.width,
                    height=info.height,
                )
            else:
                pdf = await read_pdf_async(data)
                new = NewAttachment(
                    "pdf", PDF_MIME, len(data), key, file.file_name, pages=pdf.pages
                )
        except Unreadable as exc:
            log.info("refused a %s: %s", file.kind, exc)
            return notices.unreadable(address, "image" if file.kind == "image" else "PDF")
        return _Ready(new, data)
