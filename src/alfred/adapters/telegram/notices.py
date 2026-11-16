"""Fixed notices about messages alfred can't read, sent without the model.

Like every notice, they speak in the butler's register and end their first
clause with the user's form of address, used exactly as configured; they are
not configurable, even with a custom persona (AGENTS.md, "Notices").
"""

from __future__ import annotations


def text_only(address: str) -> str:
    """Anything but text, when attachments are not configured."""
    return f"I'm afraid I can only read text messages, {address}."


def unsupported(address: str) -> str:
    """A message type alfred can't read (voice, sticker, other files...)."""
    return f"I can read text, images and PDFs, {address}."
