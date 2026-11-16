"""The notice that the chosen brain failed, sent without the model.

Like every notice, they speak in the butler's register and end their first
clause with the user's form of address, used exactly as configured; they are
not configurable, even with a custom persona (AGENTS.md, "Notices").
"""

from __future__ import annotations


def fell_back(address: str, failed: str, default: str) -> str:
    """The chosen brain failed and alfred went back to the startup brain."""
    return f"The brain {failed} failed, so I have gone back to {default}, {address}."
