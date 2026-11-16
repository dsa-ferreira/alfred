"""Turn kinds: chat, ``/ask`` and reminder, one module each.

Each kind owns its tag (``queue_items.kind``), its payload type, how it
builds its turn, its prompt framing, its question title and its decisions
about the reply, a failure and what the exchange records
(:class:`alfred.runtime.turns.kinds.base.TurnKind`). Nothing outside this package
branches on kind; toolsets check the kernel's turn types for what to offer.

:class:`Kinds` finds a kind by tag (decoding a queue item) or by turn type
(everything after). The payload types are the same for every registry, so
decoding and tagging a payload need none (:func:`tag_of`,
:func:`payload_from_json`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from alfred.runtime.turns.kinds.ask import AskKind
from alfred.runtime.turns.kinds.chat import ChatKind
from alfred.runtime.turns.kinds.reminder import ReminderKind

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    from alfred.kernel.turn import TurnInput
    from alfred.runtime.turns.kinds.base import TurnKind

# Every kind's class, for its tag and payload type.
_KIND_TYPES: tuple[type[Any], ...] = (ChatKind, AskKind, ReminderKind)


class Kinds:
    """The turn kinds, built once in ``app``."""

    def __init__(self, *kinds: TurnKind[Any, Any]) -> None:
        self._by_tag = {k.tag: k for k in kinds}
        self._by_turn: dict[type, TurnKind[Any, Any]] = {k.turn_type: k for k in kinds}

    @classmethod
    def default(cls, zone: ZoneInfo) -> Kinds:
        """Every kind; reminder notices are in ``zone``."""
        return cls(ChatKind(), AskKind(), ReminderKind(zone))

    def by_tag(self, tag: str) -> TurnKind[Any, Any]:
        """The kind of a queue item tagged ``tag``."""
        return self._by_tag[tag]

    def of(self, turn: TurnInput) -> TurnKind[Any, Any]:
        """The kind of ``turn``."""
        return self._by_turn[type(turn)]


def tag_of(payload: object) -> str:
    """The tag a queue item with ``payload`` is stored under."""
    for kind in _KIND_TYPES:
        if isinstance(payload, kind.payload):
            return str(kind.tag)
    raise ValueError(f"no turn kind has the payload {type(payload).__name__}")


def payload_from_json(tag: str, data: dict[str, Any]) -> Any:
    """The payload a ``tag`` item stored as ``data``."""
    for kind in _KIND_TYPES:
        if kind.tag == tag:
            return kind.payload.from_json(data)
    raise ValueError(f"unknown queue item kind {tag!r}")
