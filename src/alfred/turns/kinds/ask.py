"""A service's ``/ask`` prompt, for one user.

Nothing is recorded until the answer: then the prompt and the reply are,
and the reply goes to the user as ``[source] reply``. A ``NO_REPLY`` answer,
or a failure, sends and records nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

from alfred.agent.framing import TimeFormat, TurnRules
from alfred.agent.prompts import is_no_reply
from alfred.kernel.turn import AskTurn
from alfred.turns.kinds.base import Ending, NewEntry, Opening, Silent

if TYPE_CHECKING:
    from alfred.agent.results import TurnFailed
    from alfred.kernel.users import User
    from alfred.turns.queue import Item

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AskPayload:
    """A service's ``/ask`` prompt."""

    source: str
    prompt: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AskPayload:
        return cls(data["source"], data["prompt"])

    def to_json(self) -> dict[str, Any]:
        return {"source": self.source, "prompt": self.prompt}


class AskKind:
    tag: ClassVar[str] = "ask"
    payload: ClassVar[type] = AskPayload
    turn_type: ClassVar[type] = AskTurn
    shows_typing: ClassVar[bool] = False

    def turn(self, item: Item[AskPayload], user: User) -> AskTurn:
        return AskTurn(item.payload.prompt, item.id, item.payload.source, user=user)

    def opening(self, item: Item[AskPayload], turn: AskTurn) -> Opening:
        return Opening()

    def framing(self, turn: AskTurn) -> _AskFraming:
        return _AskFraming(turn.source, turn.text)

    def question_title(self, turn: AskTurn) -> str:
        return f"For a request from {turn.source}:"

    def answer(self, turn: AskTurn, reply: str, now: datetime) -> str | None:
        if is_no_reply(reply):
            log.info(
                "ask %s from %s: NO_REPLY, %s not notified",
                turn.queue_item_id,
                turn.source,
                turn.user.name,
            )
            return None
        return reply

    def on_failure(self, turn: AskTurn, failure: TurnFailed, now: datetime) -> Ending:
        return Silent()

    def entries(self, turn: AskTurn, text: str) -> list[NewEntry]:
        return [
            NewEntry("ask_prompt", turn.text, source=turn.source),
            NewEntry("ask_reply", text, source=turn.source),
        ]

    def message(self, turn: AskTurn, text: str) -> str:
        return f"[{turn.source}] {text}"


@dataclass(frozen=True)
class _AskFraming:
    """The ask rules, and the prompt added after the history (it isn't recorded yet)."""

    source: str
    text: str
    rules: TurnRules = TurnRules(ask=True)
    inline_attachments: bool = False

    def input_line(self, now: datetime, times: TimeFormat) -> str | None:
        """``[Mon 9 Nov 14:05] [ask:disk-check] 93% full``: after the time now."""
        return times.stamp(f"[ask:{self.source}] {self.text}", now)
