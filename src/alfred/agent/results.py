"""What a turn ends with: a result, a failure, or a park; and a brain fallback."""

from __future__ import annotations

from dataclasses import dataclass

from alfred.agent.checkpoint import Checkpoint
from alfred.kernel.questions import InputNeed


@dataclass(frozen=True)
class Fallback:
    """The chosen brain failed and alfred went back to the startup brain."""

    failed: str
    default: str


@dataclass(frozen=True)
class Waiting:
    """A turn parked at a call that needs its user's answer (``need``, stored apart)."""

    need: InputNeed
    checkpoint: Checkpoint


@dataclass(frozen=True)
class TurnResult:
    reply: str
    reset_requested: bool
    side_effects_possible: bool
    # Told to the user (and the admin) before the reply: the brain fell back.
    fallback: Fallback | None = None
    # Set when the turn parked for its user's answer; ``reply`` is then empty.
    waiting: Waiting | None = None


class TurnFailed(Exception):
    """The turn produced no answer.

    ``reason`` is for logs and the queue row; ``notice`` is the short text
    the turn's user may be shown. ``retryable`` is true only when the AI was
    unreachable and no tool had run yet. ``fallback``, when set, is told in
    any case: the chosen brain failed in this turn.
    """

    def __init__(
        self,
        reason: str,
        notice: str,
        *,
        retryable: bool,
        side_effects_possible: bool,
        fallback: Fallback | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.notice = notice
        self.retryable = retryable
        self.side_effects_possible = side_effects_possible
        self.fallback = fallback
