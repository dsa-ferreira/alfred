"""What alfred records as its reply when a waiting turn ends unanswered.

Like every notice, they speak in the butler's register and end their first
clause with the user's form of address, used exactly as configured; they are
not configurable, even with a custom persona (AGENTS.md, "Notices").
"""

from __future__ import annotations

# Why a waiting turn ended without its user's answer.
_NOT_DONE_REASONS = {
    "new_message": "you sent a new message before answering my question",
    "expired": "my question went unanswered for too long",
    "not_delivered": "I could not send you my question",
}


def input_not_done(address: str, tool: str, reason: str) -> str:
    """Recorded as alfred's reply when a waiting turn ends unanswered (``_NOT_DONE_REASONS``)."""
    why = _NOT_DONE_REASONS.get(reason, "the request was cancelled")
    return f"Nothing was done: I did not run {tool}, as {why}, {address}."
