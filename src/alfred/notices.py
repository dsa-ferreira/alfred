"""Fixed notices sent to a user without the model.

Each one speaks in the butler's register and ends its first clause with the
form of address of the user it is sent to, used exactly as configured. They
are not configurable, even when a custom persona is.
"""

from __future__ import annotations

_PARTIAL = " Some actions may have been partly carried out."


# Why a waiting turn ended without its user's answer.
_NOT_DONE_REASONS = {
    "new_message": "you sent a new message before answering my question",
    "expired": "my question went unanswered for too long",
    "not_delivered": "I could not send you my question",
}


def text_only(address: str) -> str:
    """Anything but text, when attachments are not configured."""
    return f"I'm afraid I can only read text messages, {address}."


def unsupported(address: str) -> str:
    """A message type alfred can't read (voice, sticker, other files...)."""
    return f"I can read text, images and PDFs, {address}."


def failed(address: str, partial: bool = False) -> str:
    return f"I'm afraid that request failed, {address}." + _partial(partial)


def unreachable(address: str) -> str:
    return f"The AI is unreachable at the moment, {address}."


def unreachable_mid(address: str) -> str:
    return f"The AI became unreachable partway through that request, {address}." + _PARTIAL


def too_many_steps(address: str, partial: bool = False) -> str:
    return f"I stopped that request after too many steps, {address}." + _partial(partial)


def fell_back(address: str, failed: str, default: str) -> str:
    """The chosen brain failed and alfred went back to the startup brain."""
    return f"The brain {failed} failed, so I have gone back to {default}, {address}."


def input_not_done(address: str, tool: str, reason: str) -> str:
    """Recorded as alfred's reply when a waiting turn ends unanswered (``NOT_DONE_REASONS``)."""
    why = _NOT_DONE_REASONS.get(reason, "the request was cancelled")
    return f"Nothing was done: I did not run {tool}, as {why}, {address}."


def _partial(partial: bool) -> str:
    return _PARTIAL if partial else ""
