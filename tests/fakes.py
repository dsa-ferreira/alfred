"""Stand-ins for the worker's and the agent's collaborators, for tests that don't use them.

Each does what alfred does when there is nothing to do: no parked turns, no
reminder rows, no facts, no typing indicator. The ones that should never be
reached raise, so a test that unexpectedly needs the real thing fails loudly.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from typing import Any

from aiogram.types import CallbackQuery

from alfred.agent.prompts import KnownFact
from alfred.inputs.store import RequestRow
from alfred.kernel.questions import Question
from alfred.kernel.users import User
from alfred.reminders.store import ReminderRow


class NoInputRequests:
    """No turn is waiting for its user's answer, and none may start waiting."""

    async def running_for(self, item_id: int, user: str) -> RequestRow | None:
        return None

    async def take_answered(self, item_id: int, user: str) -> RequestRow | None:
        return None

    async def finish(self, request_id: int, user: str) -> None:
        raise AssertionError("no request to finish")

    async def park(
        self,
        item_id: int,
        user: str,
        *,
        tool: str,
        arguments: dict[str, Any],
        question: Question,
        state: dict[str, Any],
        turn: dict[str, Any],
        expires_at: datetime | timedelta,
    ) -> int:
        raise AssertionError("this test's turns never wait for an answer")

    async def set_message(self, request_id: int, user: str, message_id: int) -> None:
        raise AssertionError("no request to send")

    async def cancel(self, request_id: int, user: str, reason: str) -> list[RequestRow]:
        raise AssertionError("no request to cancel")


class QuietQueue:
    """A queue with nothing in it that has been quiet forever: the sweeper never waits."""

    def __init__(self) -> None:
        self.activity = _QuietActivity()

    async def has_pending(self) -> bool:
        return False


class NoQuestions:
    """A question outbox that is never used."""

    async def send_question(self, user: User, request_id: int, question: Question) -> int:
        raise AssertionError("this test's turns never ask a question")


class NoReminders:
    """A reminder lookup that finds nothing."""

    async def get(self, reminder_id: int) -> ReminderRow | None:
        return None


class NoFacts:
    """A fact store with no facts."""

    async def list(self, user_name: str) -> Sequence[KnownFact]:
        return []


class NoMemoryCommand:
    """A ``/memory`` command nobody sends."""

    async def answer(self, user: User, arguments: str | None) -> list[str]:
        raise AssertionError("this test never sends /memory")


async def no_taps(callback: CallbackQuery) -> None:
    """Button taps nobody makes."""
    raise AssertionError("this test never taps a button")


def no_typing(user: User) -> AbstractAsyncContextManager[None]:
    """A typing indicator that shows nothing."""
    return contextlib.nullcontext()


def ignore() -> None:
    """A signal nobody listens to (the memory sweeper's nudge)."""


class _QuietActivity:
    def __init__(self) -> None:
        self.arrived = asyncio.Event()
        self.changed = asyncio.Event()

    def quiet_for(self) -> float:
        return math.inf
