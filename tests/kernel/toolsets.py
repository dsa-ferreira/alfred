"""Toolsets for the kernel tests."""

from __future__ import annotations

from typing import Any

from alfred.kernel.toolset import AttemptContext, LocalToolset, PromptNote, ToolOutcome
from alfred.kernel.turn import AskTurn, ChatTurn
from tests.helpers import DIOGO


def definition(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "parameters": {}}}


class Echo(LocalToolset):
    def __init__(self, label: str, *names: str) -> None:
        self.label = label
        self.definitions = [definition(n) for n in names]

    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
        return ToolOutcome(True, f"{self.label}:{name}")


class Broken(LocalToolset):
    definitions = [definition("x")]

    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
        raise ValueError("boom")


CHAT = ChatTurn("hi", 1, user=DIOGO)
ASK = AskTurn("hi", 2, "disk-check", user=DIOGO)


class Noted(Echo):
    prompt_notes = (PromptNote("about one"),)


class Counting(LocalToolset):
    """Records each call; can ask its own question through ``need``."""

    def __init__(self, *names: str) -> None:
        self.definitions = [definition(n) for n in names]
        self.calls: list[tuple[str, Any]] = []
        self.resumed: list[tuple[str, Any, Any]] = []

    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
        self.calls.append((name, raw_arguments))
        return ToolOutcome(True, f"ran {name}")


class Asking(Counting):
    async def resume(
        self,
        name: str,
        raw_arguments: Any,
        state: dict[str, Any],
        answer: Any,
        attempt: AttemptContext,
    ) -> ToolOutcome:
        self.resumed.append((name, state, answer))
        return ToolOutcome(True, f"resumed {name} with {answer}")


ARGS = '{"title": "Dune", "year": 2021}'
