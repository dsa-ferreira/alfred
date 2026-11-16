"""Recording and sending what a turn kind decided: the only I/O for kinds.

```
record(item_id, user, entries)  -> the user's conversation, as the item's entries
send(item_id, user, text)       -> the outbox; logged, never raised
deliver(item_id, kind, turn, text)
                                = record(kind.entries(turn, text))
                                  then send(kind.message(turn, text))
```

**Delivery rule.** The exchange (what a turn records and sends as its
ending) completes before the item finishes, so a crash in between repeats
it rather than losing it; recording is idempotent per item and entry kind.
A notice outside the exchange (``Tell``) is sent after the item finishes,
so a crash loses it rather than repeating it. The worker keeps this order.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from alfred.features.conversations import notices as conversations_notices

if TYPE_CHECKING:
    from alfred.features.conversations.store import History
    from alfred.kernel.outbox import Outbox
    from alfred.kernel.turn import TurnInput
    from alfred.kernel.users import User
    from alfred.runtime.turns.kinds.base import NewEntry, Opening, TurnKind

log = logging.getLogger(__name__)


class Exchange:
    def __init__(self, history: History, outbox: Outbox) -> None:
        self._history = history
        self._outbox = outbox

    async def open(self, item_id: int, user: User, opening: Opening) -> None:
        """What happens when a turn first starts: the idle marker, then its entries."""
        if opening.idle_marker:
            # A conversation closed for inactivity is told before the reply.
            # Claimed before it is sent, so a retried turn doesn't send it again.
            marker = await self._history.take_idle_marker(user.name)
            if marker is not None:
                await self.send(
                    item_id, user, conversations_notices.reset_marker(*marker, idle=True)
                )
        await self.record(item_id, user, opening.entries)

    async def record(self, item_id: int, user: User, entries: Sequence[NewEntry]) -> None:
        """Record ``entries`` in ``user``'s conversation, as queue item ``item_id``'s."""
        for entry in entries:
            await self._history.append(
                user.name,
                entry.kind,
                entry.text,
                source=entry.source,
                turn_id=item_id,
                origin=entry.origin,
                attachment_ids=entry.attachment_ids,
            )

    async def send(self, item_id: int, user: User, text: str) -> None:
        """Send ``text`` to ``user``; a failure is logged, never raised."""
        try:
            await self._outbox.send(user, text)
        except Exception:
            # The turn already ran; re-running it would repeat tool actions.
            log.exception("queue item %s: could not deliver a message to %s", item_id, user.name)

    async def deliver(
        self, item_id: int, kind: TurnKind[Any, Any], turn: TurnInput, text: str
    ) -> None:
        """End the exchange with ``text``: record it, then send it."""
        await self.record(item_id, turn.user, kind.entries(turn, text))
        await self.send(item_id, turn.user, kind.message(turn, text))
