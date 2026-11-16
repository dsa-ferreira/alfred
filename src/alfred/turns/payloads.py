"""What each kind of queue item carries, stored as ``queue_items.payload``.

The kind is the payload's type: ``chat``, ``ask`` or ``reminder``. Each
payload round-trips through JSON; the kind that reads it is its only
reader, and whoever queues it its only writer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Literal

TurnKind = Literal["chat", "ask", "reminder"]


@dataclass(frozen=True)
class ChatPayload:
    """A user's message, with the ids of its attachments in message order."""

    kind: ClassVar[TurnKind] = "chat"

    text: str
    attachment_ids: tuple[int, ...] = ()

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ChatPayload:
        return cls(data["text"], tuple(data["attachment_ids"]))

    def to_json(self) -> dict[str, Any]:
        return {"text": self.text, "attachment_ids": list(self.attachment_ids)}


@dataclass(frozen=True)
class AskPayload:
    """A service's ``/ask`` prompt."""

    kind: ClassVar[TurnKind] = "ask"

    source: str
    prompt: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AskPayload:
        return cls(data["source"], data["prompt"])

    def to_json(self) -> dict[str, Any]:
        return {"source": self.source, "prompt": self.prompt}


@dataclass(frozen=True)
class ReminderPayload:
    """A reminder's occurrence, snapshotted when it fired.

    A reminder's text, creator, rule and quietness never change once it is
    set, so the snapshot is what the reminder says.
    """

    kind: ClassVar[TurnKind] = "reminder"

    reminder_id: int
    # When this occurrence was due (a repeating reminder's row has moved on).
    due_at: datetime
    text: str
    created_by: str
    # The repeat rule as said to the model ("every weekday at 08:00"); None for a one-shot.
    repeat_label: str | None = None
    quiet_ok: bool = False

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ReminderPayload:
        return cls(
            data["reminder_id"],
            datetime.fromisoformat(data["due_at"]),
            data["text"],
            data["created_by"],
            data["repeat_label"],
            data["quiet_ok"],
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "reminder_id": self.reminder_id,
            "due_at": self.due_at.isoformat(),
            "text": self.text,
            "created_by": self.created_by,
            "repeat_label": self.repeat_label,
            "quiet_ok": self.quiet_ok,
        }


Payload = ChatPayload | AskPayload | ReminderPayload


def payload_from_json(kind: str, data: dict[str, Any]) -> Payload:
    """The payload a ``kind`` item stored as ``data``."""
    match kind:
        case "chat":
            return ChatPayload.from_json(data)
        case "ask":
            return AskPayload.from_json(data)
        case "reminder":
            return ReminderPayload.from_json(data)
    raise ValueError(f"unknown queue item kind {kind!r}")
