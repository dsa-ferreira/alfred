"""A question's title: what asked for it, in plain text."""

from __future__ import annotations


def title(kind: str, *, source: str | None = None, reminder: str | None = None) -> str:
    """What asked, by the turn's kind: ``chat``, ``ask`` or ``reminder``.

    ``reminder`` is ``#12 (call mum)``.
    """
    if kind == "ask":
        return f"For a request from {source}:"
    if kind == "reminder":
        return f"For reminder {reminder}:"
    return "For your request:"


def reminder_label(reminder_id: int | None, text: str) -> str:
    short = text if len(text) <= 60 else text[:59] + "…"
    return f"#{reminder_id} ({short})"
