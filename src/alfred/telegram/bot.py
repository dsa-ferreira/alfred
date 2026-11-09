"""The aiogram bot: owner-only private chat, text and files in, replies out.

alfred defines no bot commands. Every owner text message, including one that
starts with ``/``, is queued for the AI exactly as written.

With attachments configured (change add-attachments), photos, ``image/*``
files and PDF files go to the intake with their caption; anything else gets
the ``unsupported`` notice. Without them, anything but text gets
``text_only``. The parts of an album arrive as separate messages sharing a
``media_group_id``: they are collected until no part has come for
``album_wait_s`` and then received as one message (design D3).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from aiogram import Bot, Dispatcher
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from alfred import notices
from alfred.attachments.intake import DownloadFailed, IncomingFile, Intake
from alfred.queue import Queue
from alfred.telegram.formatting import split_for_telegram

log = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message"]

# Telegram shows a chat action for about 5 seconds; resend a little sooner so
# a slow request leaves no gap (design D2).
TYPING_INTERVAL_S = 4.0


# Seconds for one file download from Telegram (at most 20 MB).
DOWNLOAD_TIMEOUT_S = 120
PDF_MIME = "application/pdf"


def incoming_file(message: Message) -> IncomingFile | None:
    """The message's photo, image file or PDF file; ``None`` for anything else."""
    if message.photo:
        largest = message.photo[-1]  # Telegram lists the sizes smallest first
        return IncomingFile("image", largest.file_id, largest.file_size)
    document = message.document
    if document is None:
        return None
    mime = (document.mime_type or "").lower()
    if mime == PDF_MIME:
        return IncomingFile("pdf", document.file_id, document.file_size, document.file_name)
    if mime.startswith("image/"):
        return IncomingFile("image", document.file_id, document.file_size, document.file_name)
    return None


class TelegramDownloader:
    """Downloads a file the owner sent, into memory."""

    def __init__(self, bot: Bot, timeout_s: int = DOWNLOAD_TIMEOUT_S) -> None:
        self._bot = bot
        self._timeout_s = timeout_s

    async def download(self, file_id: str) -> bytes:
        try:
            out = await self._bot.download(file_id, timeout=self._timeout_s)
        except Exception as exc:
            # Only the type: the download URL, which errors may quote,
            # contains the bot token.
            raise DownloadFailed(type(exc).__name__) from None
        if out is None:
            raise DownloadFailed("nothing downloaded")
        return out.read()


class AlbumCollector:
    """Holds the parts of each album until ``wait_s`` passes without a new one."""

    def __init__(self, wait_s: float, receive: Callable[[list[Message]], Awaitable[None]]) -> None:
        self._wait_s = wait_s
        self._receive = receive
        self._parts: dict[str, list[Message]] = {}
        self._timers: dict[str, asyncio.Task[None]] = {}

    def add(self, message: Message) -> None:
        group = message.media_group_id
        assert group is not None
        self._parts.setdefault(group, []).append(message)
        timer = self._timers.get(group)
        if timer is not None:
            timer.cancel()
        self._timers[group] = asyncio.create_task(self._later(group), name=f"album {group}")

    async def drain(self) -> None:
        """Wait for every album collected so far to be received (for tests)."""
        while self._timers:
            await asyncio.gather(*self._timers.values(), return_exceptions=True)

    async def _later(self, group: str) -> None:
        await asyncio.sleep(self._wait_s)
        # From here on the album is complete: a cancel can no longer reach it.
        self._timers.pop(group, None)
        parts = sorted(self._parts.pop(group, []), key=lambda m: m.message_id)
        try:
            await self._receive(parts)
        except Exception:
            log.exception("album %s could not be received", group)


def make_dispatcher(
    owner_user_id: int,
    queue: Queue,
    owner_address: str,
    *,
    intake: Intake | None = None,
    album_wait_s: float = 2,
) -> Dispatcher:
    dispatcher = Dispatcher()

    async def receive(parts: list[Message]) -> None:
        """One message or one album: the usable files with the caption, notices for the rest."""
        assert intake is not None
        files: list[IncomingFile] = []
        refused = 0
        for part in parts:
            file = incoming_file(part)
            if file is None:
                refused += 1
            else:
                files.append(file)
        caption = next((p.caption for p in parts if p.caption), "")
        texts = [notices.unsupported(owner_address)] * refused
        if files:
            received = await intake.receive(caption, files, parts[0].message_id)
            texts += received.notices
        for text in texts:
            await parts[0].answer(text, parse_mode=None)

    albums = AlbumCollector(album_wait_s, receive)
    dispatcher["albums"] = albums

    @dispatcher.message()
    async def on_message(message: Message) -> None:
        user = message.from_user
        if user is None or user.id != owner_user_id or message.chat.type != ChatType.PRIVATE:
            # Only the sender's ID is logged, never the message.
            log.info(
                "ignored a message from user %s in a %s chat",
                user.id if user else "unknown",
                message.chat.type,
            )
            return
        if message.text is not None:
            item_id = await queue.enqueue_owner(message.text, message.message_id)
            log.info("queued owner message %s as item %s", message.message_id, item_id)
            return
        if intake is None:
            await message.answer(notices.text_only(owner_address), parse_mode=None)
            return
        if message.media_group_id is not None:
            albums.add(message)
            return
        await receive([message])

    return dispatcher


class TelegramOutbox:
    """Sends text to the owner's chat, formatted and split for Telegram.

    Raises when Telegram doesn't accept a message, so callers can react
    (``/notify`` answers 502).
    """

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id

    async def send(self, text: str) -> None:
        for markdown, html in split_for_telegram(text):
            try:
                await self._bot.send_message(self._chat_id, html, parse_mode="HTML")
            except TelegramBadRequest as exc:
                if "can't parse entities" not in str(exc).lower():
                    raise
                log.warning("Telegram rejected the formatting; resending as plain text")
                await self._bot.send_message(self._chat_id, markdown, parse_mode=None)

    @contextlib.asynccontextmanager
    async def typing(self) -> AsyncIterator[None]:
        """Show "typing..." in the owner's chat until the block exits.

        Failures to send the action are logged and never raised, so they
        can't affect the turn the block wraps (design D3).
        """
        task = asyncio.create_task(self._keep_typing())
        try:
            yield
        finally:
            task.cancel()
            # wait() doesn't re-raise the task's cancellation, but a
            # cancellation of the caller still propagates.
            await asyncio.wait([task])

    async def _keep_typing(self) -> None:
        failed = False
        while True:
            try:
                await self._bot.send_chat_action(self._chat_id, ChatAction.TYPING)
            except Exception as exc:
                level = logging.DEBUG if failed else logging.WARNING
                log.log(
                    level, "could not send the typing indicator: %s: %s", type(exc).__name__, exc
                )
                failed = True
            await asyncio.sleep(TYPING_INTERVAL_S)
