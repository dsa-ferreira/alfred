"""``[reminders]``."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator, Field

from alfred.infra.settings import Section

_CLOCK = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")


def _check_clock(value: str) -> str:
    if not _CLOCK.fullmatch(value):
        raise ValueError("must be a 24-hour time HH:MM, such as 09:00")
    return value


class RemindersConfig(Section):
    """Timed reminders."""

    # The time of a reminder on a date with no clock time ("on Friday").
    default_time: Annotated[str, AfterValidator(_check_clock)] = "09:00"
    # Checks run on the clock: at every whole minute and every
    # check_interval_s within it, so at most 60.
    check_interval_s: Annotated[float, Field(gt=0, le=60)] = 30
