"""Recording the turns whose questions ended unanswered.

```
for each ended request:
    turn  = its item's kind builds the turn
    text  = input_not_done(address, tool, reason)
    record(kind.entries(turn, text))       not sent: the question message is edited
```

Written by alfred, so the model sees in later turns what was not done. The
input-request store only ends requests and returns them; each path that
ends one calls :meth:`Endings.record` once its transaction has committed: a
user's new message and the expiry loop (``turns.inputs.loop``), a tap past
the expiry (``telegram.taps``) and a question that couldn't be sent
(``turns.inputs.parking``).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from alfred.turns.inputs import notices
from alfred.turns.queue import read_item

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from alfred.kernel.users import Users
    from alfred.turns.exchange import Exchange
    from alfred.turns.inputs.store import RequestRow
    from alfred.turns.kinds import Kinds

log = logging.getLogger(__name__)


class Endings:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        exchange: Exchange,
        kinds: Kinds,
        users: Users,
    ) -> None:
        self._sessions = sessions
        self._exchange = exchange
        self._kinds = kinds
        self._users = users

    async def record(self, ended: Sequence[RequestRow]) -> None:
        """Record each ended request's turn through its kind; failures are only logged."""
        for request in ended:
            try:
                await self._record(request)
            except Exception:
                log.exception("input request %s: could not record its ending", request.id)

    async def _record(self, request: RequestRow) -> None:
        async with self._sessions() as session:
            item = await read_item(session, request.queue_item_id)
        if item is None:
            return
        user = self._users.get(request.user_name)
        if user is None:
            log.warning(
                "input request %s: user %r is not configured; its ending is not recorded",
                request.id,
                request.user_name,
            )
            return
        log.info(
            "input request %s (item %s, %s): ended unanswered (%s)",
            request.id,
            item.id,
            user.name,
            request.cancel_reason,
        )
        kind = self._kinds.by_tag(item.kind)
        turn = kind.turn(item, user)
        text = notices.input_not_done(user.address, request.tool, request.cancel_reason or "")
        await self._exchange.record(item.id, user, kind.entries(turn, text))
