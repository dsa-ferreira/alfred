"""Where a turn's context comes from, in characters (logged, never sent)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from alfred.tools.base import LOCAL_GROUP, OpenToolset

LARGEST_TOOLS = 5


def _chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def context_sizes(messages: list[dict[str, Any]], opened: Sequence[OpenToolset]) -> str:
    """The sizes of the system prompt, the tools by group and the history, without their text.

    ``messages`` is the system prompt followed by the history (and the ask
    input), as built at the start of the attempt. Notes with a log label (a
    server's instructions) are listed inside the system prompt's size; tool
    sizes are each definition's JSON, grouped by toolset, with alfred's own
    tools last.
    """
    system = len(messages[0]["content"])
    line = f"system {system} chars"
    labelled = [n for t in opened for n in t.prompt_notes if n.log_label is not None]
    if labelled:
        parts = ", ".join(f"{n.log_label} {n.log_chars}" for n in labelled)
        line += f" (instructions: {parts})"

    by_group: dict[str, int] = {}
    local = 0
    count = 0
    for toolset in opened:
        for definition in toolset.definitions:
            count += 1
            group = toolset.group_of(definition["function"]["name"])
            if group == LOCAL_GROUP:
                local += _chars(definition)
            else:
                by_group[group] = by_group.get(group, 0) + _chars(definition)
    if local:
        by_group[LOCAL_GROUP] = local
    total = sum(by_group.values())
    line += f"; tools {count} defs {total} chars"
    if by_group:
        line += " (" + ", ".join(f"{g} {n}" for g, n in by_group.items()) + ")"

    history = messages[1:]
    return line + f"; history {len(history)} entries {_chars(history)} chars"


def largest_tools(definitions: list[dict[str, Any]], count: int = LARGEST_TOOLS) -> str:
    """The ``count`` largest tool definitions as ``name size``, largest first."""
    sizes = sorted(((d["function"]["name"], _chars(d)) for d in definitions), key=lambda t: -t[1])
    return ", ".join(f"{name} {size}" for name, size in sizes[:count]) or "none"
