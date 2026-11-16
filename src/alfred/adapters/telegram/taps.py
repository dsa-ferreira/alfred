"""Button taps on question messages.

```
tap -> the sender is a configured user, in their private chat?   (else: log the ID, ignore)
    -> InputRequests.answer(request, option, user)
         answered  -> wake the worker; edit the message to the outcome, no buttons
         not_open  -> "no longer open" (an expired one is ended, its turn recorded
                      and its message edited)
         not_yours -> refused; logged with the tapper's ID only
    -> always answer the callback, so the client stops showing it as loading
```
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from aiogram.enums import ChatType
from aiogram.types import CallbackQuery

from alfred.adapters.telegram.questions import (
    answered_line,
    ended_html,
    outcome_html,
    parse_callback,
)
from alfred.kernel.users import User, Users
from alfred.runtime.turns.inputs.endings import Endings
from alfred.runtime.turns.inputs.store import InputRequests

log = logging.getLogger(__name__)

_NO_LONGER_OPEN = "That question is no longer open."
_NOT_YOURS = "That question isn't yours to answer."


class Taps:
    def __init__(
        self,
        users: Users,
        requests: InputRequests,
        endings: Endings,
        outbox: _QuestionEditor,
        notify: Callable[[], None],
    ) -> None:
        self._users = users
        self._requests = requests
        self._endings = endings
        self._outbox = outbox
        # Wakes the worker (``QueueActivity.notify``).
        self._notify = notify

    async def __call__(self, callback: CallbackQuery) -> None:
        user = self._sender(callback)
        if user is None:
            # Only the sender's ID is logged, never what they tapped.
            log.info("ignored a button tap from user %s", callback.from_user.id)
            await self._acknowledge(callback)
            return
        parsed = parse_callback(callback.data)
        if parsed is None:
            await self._acknowledge(callback, _NO_LONGER_OPEN)
            return
        request_id, option = parsed
        result = await self._requests.answer(request_id, option, user.name)
        if result.outcome == "not_yours":
            log.info("refused a tap on request %s from user %s", request_id, user.telegram_user_id)
            await self._acknowledge(callback, _NOT_YOURS)
            return
        if result.outcome == "not_open":
            await self._acknowledge(callback, _NO_LONGER_OPEN)
            ended = result.request
            if result.expired and ended is not None:
                await self._endings.record([ended])
            if result.expired and ended is not None and ended.telegram_message_id is not None:
                html = ended_html(ended.question, "expired")
                await self._outbox.edit_question(user, ended.telegram_message_id, html)
            return
        assert result.request is not None and result.label is not None
        log.info("%s answered request %s: %s", user.name, request_id, result.label)
        self._notify()
        await self._acknowledge(callback, result.label)
        message_id = result.request.telegram_message_id
        if message_id is None and callback.message is not None:
            message_id = callback.message.message_id
        if message_id is not None:
            html = outcome_html(result.request.question, answered_line(result.label))
            await self._outbox.edit_question(user, message_id, html)

    def _sender(self, callback: CallbackQuery) -> User | None:
        """The configured user who tapped, in their private chat with the bot, if any."""
        message = callback.message
        if message is None or message.chat.type != ChatType.PRIVATE:
            return None
        return self._users.by_telegram_id(callback.from_user.id)

    async def _acknowledge(self, callback: CallbackQuery, text: str | None = None) -> None:
        try:
            await callback.answer(text)
        except Exception as exc:
            log.warning("could not answer a button tap: %s", type(exc).__name__)


class _QuestionEditor(Protocol):
    async def edit_question(self, user: User, message_id: int, html: str) -> None: ...
