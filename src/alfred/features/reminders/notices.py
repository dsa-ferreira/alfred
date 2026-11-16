"""A reminder delivered without the model.

Like every notice, they speak in the butler's register and end their first
clause with the user's form of address, used exactly as configured; they are
not configurable, even with a custom persona (AGENTS.md, "Notices").
"""

from __future__ import annotations

from datetime import date, datetime


def reminder(address: str, text: str, due: datetime, today: date) -> str:
    """A reminder delivered without the model.

    ``due`` is in alfred's timezone; its date is shown when it isn't ``today``.
    """
    when = f"{due:%H:%M}"
    if due.date() != today:
        when += f" on {due:%A} {due.day} {due:%B}"
    return (
        f'Your reminder, due at {when}: "{text}". My apologies for the plain delivery, {address}.'
    )
