"""Fixed notices about a turn that failed, sent without the model.

Like every notice, they speak in the butler's register and end their first
clause with the user's form of address, used exactly as configured; they are
not configurable, even with a custom persona (AGENTS.md, "Notices").
"""

from __future__ import annotations

_PARTIAL = " Some actions may have been partly carried out."


def failed(address: str, partial: bool = False) -> str:
    return f"I'm afraid that request failed, {address}." + _partial(partial)


def unreachable(address: str) -> str:
    return f"The AI is unreachable at the moment, {address}."


def unreachable_mid(address: str) -> str:
    return f"The AI became unreachable partway through that request, {address}." + _PARTIAL


def too_many_steps(address: str, partial: bool = False) -> str:
    return f"I stopped that request after too many steps, {address}." + _partial(partial)


def _partial(partial: bool) -> str:
    return _PARTIAL if partial else ""
