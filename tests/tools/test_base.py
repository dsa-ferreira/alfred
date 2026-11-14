from __future__ import annotations

from typing import Any

import pytest

from alfred.tools.base import (
    AllowList,
    AttemptContext,
    Gated,
    LocalToolset,
    PromptNote,
    ToolOutcome,
    TurnInput,
    chat_only,
    dispatch,
    safe_call,
)
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


CHAT = TurnInput("chat", "hi", 1, user=DIOGO)
ASK = TurnInput("ask", "hi", 2, "disk-check", user=DIOGO)


async def test_first_toolset_that_handles_the_name_wins() -> None:
    opened = [Echo("a", "one"), Echo("b", "one", "two")]
    ctx = AttemptContext(CHAT)
    assert await dispatch(opened, "one", None, ctx) == ToolOutcome(True, "a:one")
    assert await dispatch(opened, "two", None, ctx) == ToolOutcome(True, "b:two")


async def test_unknown_tool() -> None:
    outcome = await dispatch([Echo("a", "one")], "nope", None, AttemptContext(CHAT))
    assert outcome == ToolOutcome(False, "Error: unknown tool 'nope'.")


async def test_exception_becomes_an_error_result(caplog: pytest.LogCaptureFixture) -> None:
    outcome = await safe_call(Broken(), "x", None, AttemptContext(CHAT))
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


# --- Allow-list gate (change add-multiple-users) ---------------------------------


@pytest.mark.parametrize(
    ("entries", "name", "matches"),
    [
        (("alfred_web_search",), "alfred_web_search", True),
        (("alfred_web_search",), "alfred_web_fetch", False),
        (("alfred_web_search",), "alfred_web_search_more", False),
        (("alfred_*",), "alfred_remind_set", True),
        (("alfred_*",), "cookbook_get_recipe", False),
        (("cookbook_*",), "cookbook_get_recipe", True),
        (("mcparr_find_media", "cookbook_*"), "mcparr_find_media", True),
        (("mcparr_find_media", "cookbook_*"), "mcparr_add_media", False),
        (("*",), "anything_at_all", True),
        ((), "alfred_calculate", False),
    ],
)
def test_allow_list_matching(entries: tuple[str, ...], name: str, matches: bool) -> None:
    assert AllowList(entries).matches(name) is matches


@pytest.mark.parametrize(
    ("entries", "prefix", "may"),
    [
        (("*",), "cookbook_", True),
        (("cookbook_*",), "cookbook_", True),
        (("cookbook_get_recipe",), "cookbook_", True),
        (("alfred_*", "mcparr_find_media"), "cookbook_", False),
        ((), "cookbook_", False),
    ],
)
def test_allow_list_may_match_a_prefix(entries: tuple[str, ...], prefix: str, may: bool) -> None:
    assert AllowList(entries).may_match_prefix(prefix) is may


def test_allow_list_exact_entries() -> None:
    allow = AllowList.of("alfred_*", "mcparr_find_media", "mcparr_add_*", "cookbook_get")
    assert allow.exact("mcparr_") == ["mcparr_find_media"]
    assert allow.exact() == ["mcparr_find_media", "cookbook_get"]


class Noted(Echo):
    prompt_notes = (PromptNote("about one"),)


def test_gate_hides_definitions() -> None:
    gated = Gated(Noted("a", "alfred_one", "alfred_two", "other_three"), AllowList.of("alfred_*"))
    assert [d["function"]["name"] for d in gated.definitions] == ["alfred_one", "alfred_two"]
    assert gated.handles("alfred_one") and not gated.handles("other_three")
    assert [n.text for n in gated.prompt_notes] == ["about one"]
    assert gated.group_of("alfred_one") == "alfred"


def test_empty_allow_list_offers_nothing() -> None:
    gated = Gated(Echo("a", "alfred_one"), AllowList())
    assert gated.definitions == [] and not gated.handles("alfred_one")


async def test_gated_name_is_an_unknown_tool() -> None:
    called: list[str] = []

    class Recording(Echo):
        async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
            called.append(name)
            return await super().call(name, raw_arguments, attempt)

    opened = [Gated(Recording("a", "alfred_one", "secret_two"), AllowList.of("alfred_*"))]
    outcome = await dispatch(opened, "secret_two", None, AttemptContext(CHAT))
    assert not outcome.ok and outcome.text == "Error: unknown tool 'secret_two'."
    assert called == []
    assert (await dispatch(opened, "alfred_one", None, AttemptContext(CHAT))).text == "a:alfred_one"


def test_gate_passes_attempts_through() -> None:
    seen: list[AttemptContext] = []

    class Attempts(Echo):
        def new_attempt(self, attempt: AttemptContext) -> None:
            seen.append(attempt)

    ctx = AttemptContext(CHAT)
    Gated(Attempts("a", "x"), AllowList.of("*")).new_attempt(ctx)
    assert seen == [ctx]
