"""Fixed notices sent to a user without the model (design D5).

Each one speaks in the butler's register and ends its first clause with the
form of address of the user it is sent to, used exactly as configured. They
are not configurable, even when a custom persona is.
"""

from __future__ import annotations

from datetime import date, datetime

_PARTIAL = " Some actions may have been partly carried out."


# --- input requests (change add-user-input-requests, design D5) -------------------------

# Why a waiting turn ended without its user's answer.
_NOT_DONE_REASONS = {
    "new_message": "you sent a new message before answering my question",
    "expired": "my question went unanswered for too long",
    "not_delivered": "I could not send you my question",
}


def text_only(address: str) -> str:
    """Anything but text, when attachments are not configured."""
    return f"I'm afraid I can only read text messages, {address}."


# --- attachments (change add-attachments, design D12) ---------------------------


def unsupported(address: str) -> str:
    """A message type alfred can't read (voice, sticker, other files...)."""
    return f"I can read text, images and PDFs, {address}."


def too_large(address: str, limit_bytes: int) -> str:
    return (
        f"I'm afraid that file is too large; I accept files up to "
        f"{limit_bytes / 1_000_000:g} MB, {address}."
    )


def unreadable(address: str, what: str = "file") -> str:
    """An image that can't be decoded, or a damaged, locked or empty PDF (``what``)."""
    return f"I'm afraid I can't open that {what}, {address}."


def not_kept(address: str) -> str:
    """Downloading or storing a file failed; nothing was queued."""
    return f"I'm afraid I couldn't keep that file. Kindly send it again, {address}."


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


def reminder(address: str, text: str, due: datetime, today: date) -> str:
    """A reminder delivered without the model (change add-reminders, design D6).

    ``due`` is in alfred's timezone; its date is shown when it isn't ``today``.
    """
    when = f"{due:%H:%M}"
    if due.date() != today:
        when += f" on {due:%A} {due.day} {due:%B}"
    return (
        f'Your reminder, due at {when}: "{text}". My apologies for the plain delivery, {address}.'
    )


# --- /memory (change add-user-facts, design D11) -----------------------------------------


def memory_list(address: str) -> str:
    """Before the list of a user's facts."""
    return f"This is what I know about you, {address}:"


def memory_none(address: str) -> str:
    return f"I know no facts about you yet, {address}."


def memory_forgot(address: str, removed: list[str], missing: list[int]) -> str:
    """After ``/memory forget``: the facts removed (as listed) and the ids not found."""
    if removed:
        lines = [f"I have forgotten these, {address}:", *removed]
        if missing:
            lines.append(f"I found no fact {_numbers(missing)}.")
    else:
        lines = [f"I found no fact {_numbers(missing)}, {address}."]
    return "\n".join(lines)


def memory_usage(address: str) -> str:
    return (
        f"I'm afraid I don't follow, {address}. Send /memory to see what I know about "
        "you, or /memory forget followed by the numbers of the facts to forget, "
        "such as /memory forget 12 15."
    )


def input_not_done(address: str, tool: str, reason: str) -> str:
    """Recorded as alfred's reply when a waiting turn ends unanswered (``NOT_DONE_REASONS``)."""
    why = _NOT_DONE_REASONS.get(reason, "the request was cancelled")
    return f"Nothing was done: I did not run {tool}, as {why}, {address}."


def _partial(partial: bool) -> str:
    return _PARTIAL if partial else ""


def _numbers(ids: list[int]) -> str:
    return ", ".join(str(i) for i in ids)
