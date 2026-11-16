"""Fixed notices about a user's file, sent without the model.

Like every notice, they speak in the butler's register and end their first
clause with the user's form of address, used exactly as configured; they are
not configurable, even with a custom persona (AGENTS.md, "Notices").
"""

from __future__ import annotations


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
