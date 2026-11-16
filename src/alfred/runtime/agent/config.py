"""The agent's part of ``[ai]``: persona, limits and alfred's time zone."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from alfred.infra.settings import PositiveInt, Section, TimeZoneName


class AgentConfig(Section):
    max_requests_per_turn: PositiveInt = 12
    tool_result_max_chars: PositiveInt = 4000
    persona: Annotated[str, Field(min_length=1)] | None = None
    # alfred's timezone: today's date in the prompt, reminders and search dates.
    timezone: TimeZoneName = "Europe/Lisbon"
