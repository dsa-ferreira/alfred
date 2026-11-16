"""Retry delays shared by the queue and the background jobs."""

from __future__ import annotations

from typing import Protocol


class RetryDelays(Protocol):
    """The first retry delay and the most it doubles to (``[queue]``)."""

    @property
    def retry_initial_s(self) -> float: ...

    @property
    def retry_max_s(self) -> float: ...


def backoff_s(attempts: int, initial_s: float, max_s: float) -> float:
    """Delay before retry number ``attempts`` (1-based): doubles up to ``max_s``."""
    return min(initial_s * 2 ** (attempts - 1), max_s)
