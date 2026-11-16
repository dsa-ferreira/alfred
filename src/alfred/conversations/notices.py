"""The marker that tells a user their conversation ended."""

from __future__ import annotations


def reset_marker(archived_id: int | None, new_id: int, *, idle: bool = False) -> str:
    """The one message that tells the user a conversation ended.

    Ids only: the title is made later, in the background. ``idle`` for a conversation closed
    after inactivity.
    """
    lines: list[str] = []
    if archived_id is not None:
        how = "closed after inactivity" if idle else "archived"
        lines.append(f"— #{archived_id} {how} —")
    lines.append(f"— new conversation #{new_id} —")
    return "\n".join(lines)
