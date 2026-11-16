"""``[history]``: how much of the current conversation a turn sees, and when it closes."""

from __future__ import annotations

from alfred.infra.settings import PositiveFloat, PositiveInt, Section


class HistoryConfig(Section):
    max_entries: PositiveInt = 30
    # Hours after a user's last own message before their conversation is
    # closed. None: never.
    idle_close_after_h: PositiveFloat | None = None
