"""Retry delays shared by the queue and the memory sweeper."""

from __future__ import annotations

from alfred.config import QueueConfig


def backoff_s(attempts: int, config: QueueConfig) -> float:
    """Delay before retry number ``attempts`` (1-based): doubles up to the maximum."""
    return min(config.retry_initial_s * 2 ** (attempts - 1), config.retry_max_s)
