"""What a turn kind decides, and the decisions it hands the worker.

A :class:`TurnKind` only decides; :mod:`alfred.runtime.turns.exchange` and the worker
do. Every method is pure: the kind's turn is built from the queue item alone
(a reminder's payload is a snapshot), and the time is passed in.

- **Opening**: what is recorded when the turn first starts (not when it
  resumes), and whether a pending idle-close marker is told first.
- **Answer**: the text that ends the exchange for the model's reply, or
  ``None`` when nothing is recorded or sent.
- **Failure**: how a turn that failed for good ends (:data:`Ending`).
- **Entries** and **message**: what the exchange records for a text, and
  what its user is sent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar, Protocol

if TYPE_CHECKING:
    from alfred.features.conversations.store import EntryKind
    from alfred.features.conversations.tables import EntryOrigin
    from alfred.kernel.turn import TurnInput
    from alfred.kernel.users import User
    from alfred.runtime.agent.framing import Framing
    from alfred.runtime.agent.results import TurnFailed
    from alfred.runtime.turns.queue import Item


@dataclass(frozen=True)
class NewEntry:
    """An entry the exchange records, for the turn's user and its queue item."""

    kind: EntryKind
    text: str
    source: str | None = None
    origin: EntryOrigin | None = None
    attachment_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class Opening:
    """What happens when a turn first starts."""

    entries: tuple[NewEntry, ...] = ()
    # Tell the user's pending idle-close marker before recording.
    idle_marker: bool = False


@dataclass(frozen=True)
class Silent:
    """The turn ends with nothing recorded or sent."""


@dataclass(frozen=True)
class Tell:
    """A notice outside the exchange: sent after the item finishes, not recorded."""

    text: str


@dataclass(frozen=True)
class Deliver:
    """The exchange ends with ``text``: recorded and sent before the item finishes."""

    text: str


Ending = Silent | Tell | Deliver


class TurnKind[T: TurnInput, P](Protocol):
    # The queue item's kind (``queue_items.kind``).
    tag: ClassVar[str]
    # Its payload type, stored as JSON (``to_json`` / ``from_json``).
    payload: ClassVar[type]
    # The kernel turn type it builds.
    turn_type: ClassVar[type]
    # Whether the user's chat shows "typing" while the turn runs.
    shows_typing: ClassVar[bool]

    def turn(self, item: Item[P], user: User) -> T:
        """The turn the item asks for, ``user``'s."""
        ...

    def opening(self, item: Item[P], turn: T) -> Opening:
        """What is recorded when the turn first starts."""
        ...

    def framing(self, turn: T) -> Framing:
        """How the agent frames the turn for the model."""
        ...

    def question_title(self, turn: T) -> str:
        """The title of a question the turn asks its user: what asked for it."""
        ...

    def answer(self, turn: T, reply: str, now: datetime) -> str | None:
        """The text that ends the exchange for the model's ``reply``; ``None`` for silence."""
        ...

    def on_failure(self, turn: T, failure: TurnFailed, now: datetime) -> Ending:
        """How a turn that failed for good ends."""
        ...

    def entries(self, turn: T, text: str) -> list[NewEntry]:
        """What the exchange records, ending with ``text``."""
        ...

    def message(self, turn: T, text: str) -> str:
        """What the user is sent for ``text``."""
        ...
