"""A user's chat message.

The message is recorded when its turn starts (once, even if retried), so it
is the newest entry of its own context; a conversation closed for
inactivity is told first. The reply is recorded and sent. A failure sends
the failure notice after the item finishes, unrecorded. The typing
indicator shows while it runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

from alfred.kernel.turn import ChatTurn
from alfred.runtime.agent.framing import TimeFormat, TurnRules
from alfred.runtime.turns.kinds.base import Ending, NewEntry, Opening, Tell

if TYPE_CHECKING:
    from alfred.kernel.users import User
    from alfred.runtime.agent.results import TurnFailed
    from alfred.runtime.turns.queue import Item


@dataclass(frozen=True)
class ChatPayload:
    """A user's message, with the ids of its attachments in message order."""

    text: str
    attachment_ids: tuple[int, ...] = ()

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ChatPayload:
        return cls(data["text"], tuple(data["attachment_ids"]))

    def to_json(self) -> dict[str, Any]:
        return {"text": self.text, "attachment_ids": list(self.attachment_ids)}


class ChatKind:
    tag: ClassVar[str] = "chat"
    payload: ClassVar[type] = ChatPayload
    turn_type: ClassVar[type] = ChatTurn
    shows_typing: ClassVar[bool] = True

    def turn(self, item: Item[ChatPayload], user: User) -> ChatTurn:
        return ChatTurn(item.payload.text, item.id, user=user)

    def opening(self, item: Item[ChatPayload], turn: ChatTurn) -> Opening:
        message = NewEntry("user", item.payload.text, attachment_ids=item.payload.attachment_ids)
        return Opening((message,), idle_marker=True)

    def framing(self, turn: ChatTurn) -> _ChatFraming:
        return _ChatFraming()

    def question_title(self, turn: ChatTurn) -> str:
        return "For your request:"

    def answer(self, turn: ChatTurn, reply: str, now: datetime) -> str | None:
        return reply

    def on_failure(self, turn: ChatTurn, failure: TurnFailed, now: datetime) -> Ending:
        return Tell(failure.notice)

    def entries(self, turn: ChatTurn, text: str) -> list[NewEntry]:
        return [NewEntry("assistant", text)]

    def message(self, turn: ChatTurn, text: str) -> str:
        return text


@dataclass(frozen=True)
class _ChatFraming:
    """No turn rules or input line: the message is already the history's newest entry."""

    rules: TurnRules = TurnRules()
    inline_attachments: bool = True

    def input_line(self, now: datetime, times: TimeFormat) -> str | None:
        return None
