"""A reminder delivered without the model."""

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
