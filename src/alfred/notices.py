"""Fixed notices sent to the owner without the model (design D5).

Each one speaks in the butler's register and ends its first clause with the
owner's form of address, used exactly as configured. They are not
configurable, even when a custom persona is.
"""

from __future__ import annotations

PARTIAL = " Some actions may have been partly carried out."


def _partial(partial: bool) -> str:
    return PARTIAL if partial else ""


def text_only(address: str) -> str:
    return f"I'm afraid I can only read text messages, {address}."


def failed(address: str, partial: bool = False) -> str:
    return f"I'm afraid that request failed, {address}." + _partial(partial)


def unreachable(address: str) -> str:
    return f"The AI is unreachable at the moment, {address}."


def unreachable_mid(address: str) -> str:
    return f"The AI became unreachable partway through that request, {address}." + PARTIAL


def too_many_steps(address: str, partial: bool = False) -> str:
    return f"I stopped that request after too many steps, {address}." + _partial(partial)
