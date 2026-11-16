"""Fixed ``/memory`` notices, sent without the model.

Like every notice, they speak in the butler's register and end their first
clause with the user's form of address, used exactly as configured; they are
not configurable, even with a custom persona (AGENTS.md, "Notices").
"""

from __future__ import annotations


def memory_list(address: str) -> str:
    """Before the list of a user's facts."""
    return f"This is what I know about you, {address}:"


def memory_none(address: str) -> str:
    return f"I know no facts about you yet, {address}."


def memory_forgot(address: str, removed: list[str], missing: list[int]) -> str:
    """After ``/memory forget``: the facts removed (as listed) and the ids not found."""
    if removed:
        lines = [f"I have forgotten these, {address}:", *removed]
        if missing:
            lines.append(f"I found no fact {_numbers(missing)}.")
    else:
        lines = [f"I found no fact {_numbers(missing)}, {address}."]
    return "\n".join(lines)


def memory_usage(address: str) -> str:
    return (
        f"I'm afraid I don't follow, {address}. Send /memory to see what I know about "
        "you, or /memory forget followed by the numbers of the facts to forget, "
        "such as /memory forget 12 15."
    )


def _numbers(ids: list[int]) -> str:
    return ", ".join(str(i) for i in ids)
