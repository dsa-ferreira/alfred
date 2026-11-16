"""``[queue]`` (retry delays) and ``[input_requests]`` (waiting turns)."""

from __future__ import annotations

from pydantic import model_validator

from alfred.settings import PositiveFloat, Section


class QueueConfig(Section):
    retry_initial_s: PositiveFloat = 30
    retry_max_s: PositiveFloat = 900

    @model_validator(mode="after")
    def _max_not_below_initial(self) -> QueueConfig:
        if self.retry_max_s < self.retry_initial_s:
            raise ValueError("retry_max_s must not be smaller than retry_initial_s")
        return self


class InputRequestsConfig(Section):
    """Turns waiting for their user's answer."""

    # Counted from when the question was sent.
    expire_after_min: PositiveFloat = 60
