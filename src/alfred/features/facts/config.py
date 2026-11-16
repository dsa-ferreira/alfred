"""The facts' part of ``[memory]``."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from alfred.infra.settings import NonNegativeInt, PositiveInt, Section


class FactsConfig(Section):
    # User facts. Off: facts are learned, remembered and listed by
    # /memory, but sent in no prompt.
    facts_in_context: bool = False
    # Most characters of facts in one system prompt.
    facts_context_max_chars: PositiveInt = 3000
    # Longest fact; longer learned facts are cut, longer remembered ones refused.
    fact_max_chars: PositiveInt = 300
    # Failed extractions of one conversation before giving up. 0: never.
    facts_extraction_max_attempts: NonNegativeInt = 0
    # Cosine distance within which a user's facts are checked for duplicates by
    # the model. Distance alone decides nothing.
    facts_duplicate_max_distance: Annotated[float, Field(gt=0, lt=1)] = 0.2
