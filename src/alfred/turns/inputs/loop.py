"""Ending requests nobody answered: a user's new message, and the expiry loop.

```
new message from a user  -> cancel their open requests, then queue the message
every check_interval_s   -> expire open requests past expires_at
either way               -> the turns' entries are recorded (InputRequests),
                            and each question message is edited to say why
```

Expiry runs on its own clock, like the reminder loop, so a waiting turn
never holds up idle close past its expiry. It uses ``[reminders]
check_interval_s``; there is no setting of its own.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from alfred.kernel.users import User, Users
from alfred.times import seconds_to_next_check
from alfred.turns.inputs.store import InputRequests, RequestRow

log = logging.getLogger(__name__)


def cancel_on_new_message(
    requests: InputRequests, outbox: _QuestionEditor, users: Users
) -> Callable[[str], Awaitable[None]]:
    """The queue's ``before_user_message``: a user's new message cancels their waiting turns."""

    async def cancel(user_name: str) -> None:
        ended = await requests.cancel_for(user_name, "new_message")
        if ended:
            log.info("%s sent a new message: cancelled %d waiting turn(s)", user_name, len(ended))
            await _edit_ended(outbox, users, ended)

    return cancel


async def run_expiry_loop(
    requests: InputRequests,
    outbox: _QuestionEditor,
    users: Users,
    interval_s: float,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> None:
    log.info("input request expiry loop started (every %gs)", interval_s)
    while True:
        try:
            await _expire_once(requests, outbox, users)
        except Exception:
            # The database may be briefly away; the next check tries again.
            log.exception("input request expiry check failed")
        await sleep(seconds_to_next_check(now(), interval_s))


async def _edit_ended(outbox: _QuestionEditor, users: Users, ended: Sequence[RequestRow]) -> None:
    """Edit each ended request's question message to say why it ended, without buttons."""
    for request in ended:
        user = users.get(request.user_name)
        if user is None or request.telegram_message_id is None:
            continue
        await outbox.ended(user, request, request.cancel_reason)


async def _expire_once(
    requests: InputRequests, outbox: _QuestionEditor, users: Users
) -> list[RequestRow]:
    ended = await requests.expire_due()
    if ended:
        log.info("%d input request(s) expired: %s", len(ended), [r.id for r in ended])
        await _edit_ended(outbox, users, ended)
    return ended


class _QuestionEditor(Protocol):
    """Where question messages are shown (Telegram)."""

    async def ended(self, user: User, request: RequestRow, reason: str | None) -> None:
        """Edit the request's question message to say it ended for ``reason``, without
        its buttons. Failures are only logged."""
        ...
