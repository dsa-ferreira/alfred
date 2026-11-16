"""The archive's part of ``[memory]``: summaries, search and recall."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from alfred.infra.settings import NonNegativeFloat, PositiveInt, Section


class ArchiveConfig(Section):
    # None: the ``[ai]`` model (see :meth:`summary_model_or`).
    summary_model: Annotated[str, Field(min_length=1)] | None = None
    recall_max_chars: PositiveInt = 32000
    summary_input_max_chars: PositiveInt = 60000
    search_results: PositiveInt = 5
    # Seconds the queue must have been quiet before a summary request
    # starts. 0: only an empty queue.
    summary_quiet_s: NonNegativeFloat = 60

    def summary_model_or(self, ai_model: str) -> str:
        """The model that summarises ended conversations: this one, else ``ai_model``."""
        return self.summary_model or ai_model
