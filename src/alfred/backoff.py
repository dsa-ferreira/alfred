"""Retry delays shared by the queue and the memory sweeper."""

from __future__ import annotations


def backoff_s(attempts: int, initial_s: float, max_s: float) -> float:
    """Delay before retry number ``attempts`` (1-based): doubles up to ``max_s``."""
    return min(initial_s * 2 ** (attempts - 1), max_s)
