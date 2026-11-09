from __future__ import annotations

from typing import Any

import pytest

from alfred.agent.toolset import (
    AttemptContext,
    LocalToolset,
    ToolOutcome,
    dispatch,
    owner_only,
    safe_call,
)
from alfred.agent.turn import TurnInput


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


OWNER = TurnInput("owner", "hi", 1)
ASK = TurnInput("ask", "hi", 2, "disk-check")


async def test_first_toolset_that_handles_the_name_wins() -> None:
    opened = [Echo("a", "one"), Echo("b", "one", "two")]
    ctx = AttemptContext(OWNER)
    assert await dispatch(opened, "one", None, ctx) == ToolOutcome(True, "a:one")
    assert await dispatch(opened, "two", None, ctx) == ToolOutcome(True, "b:two")


async def test_unknown_tool() -> None:
    outcome = await dispatch([Echo("a", "one")], "nope", None, AttemptContext(OWNER))
    assert outcome == ToolOutcome(False, "Error: unknown tool 'nope'.")


async def test_exception_becomes_an_error_result(caplog: pytest.LogCaptureFixture) -> None:
    outcome = await safe_call(Broken(), "x", None, AttemptContext(OWNER))
    assert outcome == ToolOutcome(False, "Error: tool 'x' failed: ValueError")
    assert "tool x failed" in caplog.text


async def test_local_toolset_is_offered_on_every_turn_by_default() -> None:
    echo = Echo("a", "one")
    assert await echo.open(OWNER) is echo
    assert await echo.open(ASK) is echo


async def test_owner_only() -> None:
    class OwnerEcho(Echo):
        def offered(self, turn: TurnInput) -> bool:
            return owner_only(turn)

    echo = OwnerEcho("a", "one")
    assert await echo.open(OWNER) is echo
    assert await echo.open(ASK) is None


def test_request_reset() -> None:
    ctx = AttemptContext(OWNER)
    assert not ctx.reset
    ctx.request_reset()
    assert ctx.reset
