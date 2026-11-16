from __future__ import annotations

import pytest

from alfred.kernel.toolset import (
    AttemptContext,
    ToolOutcome,
    _safe_call,
    chat_only,
    dispatch,
    dispatch_resume,
)
from alfred.kernel.turn import TurnInput
from tests.kernel.toolsets import ASK, CHAT, Broken, Counting, Echo


async def test_first_toolset_that_handles_the_name_wins() -> None:
    opened = [Echo("a", "one"), Echo("b", "one", "two")]
    ctx = AttemptContext(CHAT)
    assert await dispatch(opened, "one", None, ctx) == ToolOutcome(True, "a:one")
    assert await dispatch(opened, "two", None, ctx) == ToolOutcome(True, "b:two")


async def test_unknown_tool() -> None:
    outcome = await dispatch([Echo("a", "one")], "nope", None, AttemptContext(CHAT))
    assert outcome == ToolOutcome(False, "Error: unknown tool 'nope'.")


async def test_exception_becomes_an_error_result(caplog: pytest.LogCaptureFixture) -> None:
    outcome = await _safe_call(Broken(), "x", None, AttemptContext(CHAT))
    assert outcome == ToolOutcome(False, "Error: tool 'x' failed: ValueError")
    assert "tool x failed" in caplog.text


async def test_local_toolset_is_offered_on_every_turn_by_default() -> None:
    echo = Echo("a", "one")
    assert await echo.open(CHAT) is echo
    assert await echo.open(ASK) is echo


async def test_chat_only() -> None:
    class OwnerEcho(Echo):
        def offered(self, turn: TurnInput) -> bool:
            return chat_only(turn)

    echo = OwnerEcho("a", "one")
    assert await echo.open(CHAT) is echo
    assert await echo.open(ASK) is None


def test_request_reset() -> None:
    ctx = AttemptContext(CHAT)
    assert not ctx.reset
    ctx.request_reset()
    assert ctx.reset


async def test_resume_on_a_local_toolset_is_an_error_result() -> None:
    outcome = await dispatch_resume(
        [Counting("x")], "x", None, {"via": "mine"}, True, AttemptContext(CHAT)
    )
    assert outcome == ToolOutcome(False, "Error: tool 'x' failed: NotImplementedError")


async def test_resume_of_an_unknown_tool() -> None:
    outcome = await dispatch_resume([Counting("x")], "y", None, {}, True, AttemptContext(CHAT))
    assert outcome == ToolOutcome(False, "Error: unknown tool 'y'.")
