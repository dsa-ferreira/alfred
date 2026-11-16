"""The aiogram bot: configured users' private chats, text and files in, replies out.

Only users in ``[[users]]`` are answered, each in their private chat with the
bot (change add-multiple-users, design D6); a private chat's ID is the user's
ID, so replies go to ``telegram_user_id``. Anyone else, and every group
chat, is ignored without a reply.

alfred defines one bot command, ``/memory`` (change add-user-facts, design
D11): it lists the user's facts or forgets some, without the model, and is
neither queued nor recorded in the conversation. Every other text message
from a user, including one that starts with ``/``, is queued for the AI as
theirs exactly as written.

With attachments configured (change add-attachments), photos, ``image/*``
files and PDF files go to the intake with their caption; anything else gets
the ``unsupported`` notice. Without them, anything but text gets
``text_only``. The parts of an album arrive as separate messages sharing a
``media_group_id``: they are collected until no part has come for
``album_wait_s`` and then received as one message (design D3).

Button taps (callback queries) on alfred's question messages go to the tap
handler (change add-user-input-requests, design D7); the outbox sends those
questions with their buttons and edits them once they are over.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BotCommand, CallbackQuery, Message

from alfred import notices
from alfred.attachments.intake import DownloadFailed, IncomingFile, Intake
from alfred.config import User, Users
from alfred.inputs.render import keyboard, question_html
from alfred.queue import Queue
from alfred.telegram.formatting import split_for_telegram, split_markdown
from alfred.tools.base import Question

log = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message", "callback_query"]

# Telegram shows a chat action for about 5 seconds; resend a little sooner so
# a slow request leaves no gap (design D2).
_TYPING_INTERVAL_S = 4.0


# Seconds for one file download from Telegram (at most 20 MB).
_DOWNLOAD_TIMEOUT_S = 120
_PDF_MIME = "application/pdf"


def _incoming_file(message: Message) -> IncomingFile | None:
    """The message's photo, image file or PDF file; ``None`` for anything else."""
    if message.photo:
        largest = message.photo[-1]  # Telegram lists the sizes smallest first
        return IncomingFile("image", largest.file_id, largest.file_size)
    document = message.document
    if document is None:
        return None
    mime = (document.mime_type or "").lower()
    if mime == _PDF_MIME:
        return IncomingFile("pdf", document.file_id, document.file_size, document.file_name)
    if mime.startswith("image/"):
        return IncomingFile("image", document.file_id, document.file_size, document.file_name)
    return None


class TelegramDownloader:
    """Downloads a file a user sent, into memory."""

    def __init__(self, bot: Bot, timeout_s: int = _DOWNLOAD_TIMEOUT_S) -> None:
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


# ``/memory``, ``/memory@alfred_bot`` (from the command menu) and their
# arguments; not ``/memorybank``.
_MEMORY_COMMAND = re.compile(r"/memory(?:@\w+)?(?:\s+(.*))?", re.DOTALL)
_COMMANDS = [BotCommand(command="memory", description="What I know about you")]


async def register_commands(bot: Bot) -> None:
    """Show ``/memory`` in the clients' command menu. A failure is only logged."""
    try:
        await bot.set_my_commands(_COMMANDS)
    except Exception as exc:
        log.warning("could not register the bot commands: %s", type(exc).__name__)


class _Fact(Protocol):
    id: int
    text: str
    origin: str
    created_at: datetime


class _Facts(Protocol):
    """The user's facts (``alfred.memory.facts.FactStore``)."""

    async def list(self, user_name: str) -> Sequence[_Fact]: ...

    async def forget(
        self, user_name: str, ids: Sequence[int]
    ) -> tuple[Sequence[_Fact], Sequence[int]]: ...


class MemoryCommand:
    """Answers ``/memory`` and ``/memory forget <id>...`` for the sender's own facts."""

    def __init__(self, facts: _Facts, zone: ZoneInfo) -> None:
        self._facts = facts
        self._zone = zone

    async def answer(self, user: User, arguments: str | None) -> list[str]:
        """The messages to send, already split for Telegram."""
        words = (arguments or "").replace(",", " ").split()
        if not words:
            facts = await self._facts.list(user.name)
            log.info("listed %s's facts (%d)", user.name, len(facts))
            if not facts:
                return [notices.memory_none(user.address)]
            text = "\n".join([notices.memory_list(user.address), *map(self._line, facts)])
            return split_markdown(text)
        ids = [_fact_id(w) for w in words[1:]]
        if words[0].lower() != "forget" or not ids or None in ids:
            return [notices.memory_usage(user.address)]
        removed, missing = await self._facts.forget(user.name, [i for i in ids if i is not None])
        log.info(
            "%s forgot %d fact(s) with /memory, %d not found", user.name, len(removed), len(missing)
        )
        text = notices.memory_forgot(user.address, [self._line(f) for f in removed], list(missing))
        return split_markdown(text)

    def _line(self, fact: _Fact) -> str:
        """``[12] allergic to shellfish (told, 3 Nov)``."""
        day = fact.created_at.astimezone(self._zone)
        return f"[{fact.id}] {fact.text} ({fact.origin}, {day.day} {day:%b})"


def _fact_id(word: str) -> int | None:
    word = word.removeprefix("#")
    return int(word) if word.isdigit() else None


class _AlbumCollector:
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
    users: Users,
    queue: Queue,
    *,
    intake: Intake | None = None,
    album_wait_s: float = 2,
    memory: MemoryCommand | None = None,
    on_tap: Callable[[CallbackQuery], Awaitable[None]] | None = None,
) -> Dispatcher:
    dispatcher = Dispatcher()
    if on_tap is not None:
        tap = on_tap

        # A plain coroutine function: aiogram runs other callables in a thread.
        @dispatcher.callback_query()
        async def on_callback(callback: CallbackQuery) -> None:
            await tap(callback)

    def sender(message: Message) -> User | None:
        """The configured user who sent ``message`` in their private chat, if any."""
        if message.from_user is None or message.chat.type != ChatType.PRIVATE:
            return None
        return users.by_telegram_id(message.from_user.id)

    async def receive(parts: list[Message]) -> None:
        """One message or one album: the usable files with the caption, notices for the rest."""
        assert intake is not None
        user = sender(parts[0])
        assert user is not None  # only a user's messages get here
        files: list[IncomingFile] = []
        refused = 0
        for part in parts:
            file = _incoming_file(part)
            if file is None:
                refused += 1
            else:
                files.append(file)
        caption = next((p.caption for p in parts if p.caption), "")
        texts = [notices.unsupported(user.address)] * refused
        if files:
            received = await intake.receive(user, caption, files, parts[0].message_id)
            texts += received.notices
        for text in texts:
            await parts[0].answer(text, parse_mode=None)

    albums = _AlbumCollector(album_wait_s, receive)
    dispatcher["albums"] = albums

    @dispatcher.message()
    async def on_message(message: Message) -> None:
        user = sender(message)
        if user is None:
            # Only the sender's ID is logged, never the message.
            log.info(
                "ignored a message from user %s in a %s chat",
                message.from_user.id if message.from_user else "unknown",
                message.chat.type,
            )
            return
        if message.text is not None:
            command = _MEMORY_COMMAND.fullmatch(message.text.strip())
            if memory is not None and command is not None:
                # Not queued and not recorded: it never reaches the conversation.
                for text in await memory.answer(user, command.group(1)):
                    await message.answer(text, parse_mode=None)
                return
            item_id = await queue.enqueue_user(user.name, message.text, message.message_id)
            log.info("queued %s's message %s as item %s", user.name, message.message_id, item_id)
            return
        if intake is None:
            await message.answer(notices.text_only(user.address), parse_mode=None)
            return
        if message.media_group_id is not None:
            albums.add(message)
            return
        await receive([message])

    return dispatcher


class TelegramOutbox:
    """Sends text to a user's chat, formatted and split for Telegram.

    Raises when Telegram doesn't accept a message, so callers can react
    (``/notify`` answers 502).
    """

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send(self, user: User, text: str) -> None:
        chat_id = user.telegram_user_id  # a private chat's ID is the user's
        for markdown, html in split_for_telegram(text):
            try:
                await self._bot.send_message(chat_id, html, parse_mode="HTML")
            except TelegramBadRequest as exc:
                if "can't parse entities" not in str(exc).lower():
                    raise
                log.warning("Telegram rejected the formatting; resending as plain text")
                await self._bot.send_message(chat_id, markdown, parse_mode=None)

    async def send_question(self, user: User, request_id: int, question: Question) -> int:
        """Send a question with its buttons; returns its message ID. Raises if not delivered."""
        sent = await self._bot.send_message(
            user.telegram_user_id,
            question_html(question),
            parse_mode="HTML",
            reply_markup=keyboard(request_id, question),
        )
        return sent.message_id

    async def edit_question(self, user: User, message_id: int, html: str) -> None:
        """Replace a question message's text and remove its buttons. Failures are only logged."""
        try:
            await self._bot.edit_message_text(
                html, chat_id=user.telegram_user_id, message_id=message_id, parse_mode="HTML"
            )
        except Exception as exc:
            log.warning(
                "could not edit question message %s for %s: %s: %s",
                message_id,
                user.name,
                type(exc).__name__,
                exc,
            )

    @contextlib.asynccontextmanager
    async def typing(self, user: User) -> AsyncIterator[None]:
        """Show "typing..." in ``user``'s chat until the block exits.

        Failures to send the action are logged and never raised, so they
        can't affect the turn the block wraps (design D3).
        """
        task = asyncio.create_task(self._keep_typing(user.telegram_user_id))
        try:
            yield
        finally:
            task.cancel()
            # wait() doesn't re-raise the task's cancellation, but a
            # cancellation of the caller still propagates.
            await asyncio.wait([task])

    async def _keep_typing(self, chat_id: int) -> None:
        failed = False
        while True:
            try:
                await self._bot.send_chat_action(chat_id, ChatAction.TYPING)
            except Exception as exc:
                level = logging.DEBUG if failed else logging.WARNING
                log.log(
                    level, "could not send the typing indicator: %s: %s", type(exc).__name__, exc
                )
                failed = True
            await asyncio.sleep(_TYPING_INTERVAL_S)
