"""Where a parked turn stopped, as stored while it waits for its user's answer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from alfred.llm.chat import ToolCall


@dataclass(frozen=True)
class Checkpoint:
    """Where a parked turn stopped, stored as JSON while it waits for its user's answer.

    ``call`` is the call that asked, ``call_index`` its place in its model
    reply and ``remaining`` the calls after it, not run yet; ``tail`` the
    messages after the context (assistant messages with tool calls, and the
    results so far); ``requests_used`` and ``seq`` the model requests and tool
    calls so far; ``conversation_id`` and ``upto_entry_id`` (``None`` for no
    entries) where the context came from, and ``entry_ids`` exactly which
    entries.
    """

    call: ToolCall
    call_index: int
    remaining: tuple[ToolCall, ...]
    tail: list[dict[str, Any]]
    requests_used: int
    seq: int
    conversation_id: int | None
    upto_entry_id: int | None
    reset: bool = False
    entry_ids: tuple[int, ...] = ()

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Checkpoint:
        """Read what :meth:`to_json` wrote."""
        return cls(
            call=_call_of(data["call"]),
            call_index=data["call_index"],
            remaining=tuple(_call_of(c) for c in data["remaining"]),
            tail=data["tail"],
            requests_used=data["requests_used"],
            seq=data["seq"],
            conversation_id=data["conversation_id"],
            upto_entry_id=data["upto_entry_id"],
            reset=data["reset"],
            entry_ids=tuple(data["entry_ids"]),
        )

    def to_json(self) -> dict[str, Any]:
        """What is stored to resume the turn."""
        return {
            "call": _call_json(self.call),
            "call_index": self.call_index,
            "remaining": [_call_json(c) for c in self.remaining],
            "tail": self.tail,
            "requests_used": self.requests_used,
            "seq": self.seq,
            "conversation_id": self.conversation_id,
            "upto_entry_id": self.upto_entry_id,
            "reset": self.reset,
            "entry_ids": list(self.entry_ids),
        }


def _call_json(call: ToolCall) -> dict[str, Any]:
    return {"id": call.id, "name": call.name, "arguments": call.arguments}


def _call_of(data: dict[str, Any]) -> ToolCall:
    return ToolCall(data["id"], data["name"], data["arguments"])
