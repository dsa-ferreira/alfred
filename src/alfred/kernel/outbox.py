"""Sending text to a user's chat, whatever carries it."""

from __future__ import annotations

from typing import Protocol

from alfred.kernel.users import User


class Outbox(Protocol):
    """Sends text to a user's chat. Raises if the message couldn't be delivered."""

    async def send(self, user: User, text: str) -> None: ...
