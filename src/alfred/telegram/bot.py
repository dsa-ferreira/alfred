"""The aiogram bot: owner-only private chat, text in, replies out.

alfred defines no bot commands. Every owner text message, including one that
starts with ``/``, is queued for the AI exactly as written.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from aiogram import Bot, Dispatcher
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from alfred import notices
from alfred.queue import Queue
from alfred.telegram.formatting import split_for_telegram

log = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message"]

# Telegram shows a chat action for about 5 seconds; resend a little sooner so
# a slow request leaves no gap (design D2).
TYPING_INTERVAL_S = 4.0


def make_dispatcher(owner_user_id: int, queue: Queue, owner_address: str) -> Dispatcher:
    dispatcher = Dispatcher()

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
        if message.text is None:
            await message.answer(notices.text_only(owner_address), parse_mode=None)
            return
        item_id = await queue.enqueue_owner(message.text, message.message_id)
        log.info("queued owner message %s as item %s", message.message_id, item_id)

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
