from __future__ import annotations

from typing import Any

import pytest

from alfred.tools.base import (
    _CONFIRM,
    _DECLINED,
    AllowList,
    AttemptContext,
    Confirmed,
    Gated,
    LocalToolset,
    PromptNote,
    Question,
    ToolOutcome,
    TurnInput,
    _safe_call,
    chat_only,
    dispatch,
    dispatch_resume,
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


# --- input requests (change add-user-input-requests) -------------------------------


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


async def test_resume_on_a_local_toolset_is_an_error_result() -> None:
    outcome = await dispatch_resume(
        [Counting("x")], "x", None, {"via": "mine"}, True, AttemptContext(CHAT)
    )
    assert outcome == ToolOutcome(False, "Error: tool 'x' failed: NotImplementedError")


async def test_resume_of_an_unknown_tool() -> None:
    outcome = await dispatch_resume([Counting("x")], "y", None, {}, True, AttemptContext(CHAT))
    assert outcome == ToolOutcome(False, "Error: unknown tool 'y'.")


async def test_gated_resume_refuses_a_name_the_list_no_longer_matches() -> None:
    inner = Asking("mcparr_add_media")
    gate = Gated(inner, AllowList.of("mcparr_find_media"))
    outcome = await gate.resume("mcparr_add_media", ARGS, {"via": "mcp"}, {}, AttemptContext(CHAT))
    assert outcome == ToolOutcome(False, "Error: unknown tool 'mcparr_add_media'.")
    assert inner.resumed == []


async def test_gated_resume_passes_an_allowed_name_through() -> None:
    inner = Asking("mcparr_add_media")
    gate = Gated(inner, AllowList.of("mcparr_*"))
    outcome = await gate.resume("mcparr_add_media", ARGS, {"via": "mcp"}, 1, AttemptContext(CHAT))
    assert outcome.text == "resumed mcparr_add_media with 1"


def confirmed(inner: LocalToolset, *confirm: str, tools: str = "*") -> Confirmed:
    return Confirmed(Gated(inner, AllowList.of(tools)), AllowList.of(*confirm))


async def test_listed_call_waits_without_running() -> None:
    inner = Counting("mcparr_add_media")
    outcome = await confirmed(inner, "mcparr_add_media").call(
        "mcparr_add_media", ARGS, AttemptContext(CHAT)
    )
    assert inner.calls == []
    assert outcome.need is not None
    question = outcome.need.question
    assert "mcparr_add_media" in question.body
    assert question.detail == '{\n  "title": "Dune",\n  "year": 2021\n}'
    assert [(o.label, o.answer) for o in question.options] == [("Do it", True), ("Cancel", False)]
    assert outcome.need.state == {"via": "confirm"}
    # The question survives being stored.
    assert Question.from_json(question.to_json()) == question


async def test_unlisted_call_runs() -> None:
    inner = Counting("mcparr_find_media", "mcparr_add_media")
    outcome = await confirmed(inner, "mcparr_add_media").call(
        "mcparr_find_media", "{}", AttemptContext(CHAT)
    )
    assert outcome == ToolOutcome(True, "ran mcparr_find_media")


async def test_unparseable_arguments_go_to_the_inner_error() -> None:
    class Strict(Counting):
        async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
            self.calls.append((name, raw_arguments))
            return ToolOutcome(False, "Error: the arguments are not valid JSON.")

    inner = Strict("x")
    outcome = await confirmed(inner, "x").call("x", "{not json", AttemptContext(CHAT))
    assert outcome.need is None and not outcome.ok
    assert inner.calls == [("x", "{not json")]


async def test_resume_true_runs_the_call_once() -> None:
    inner = Counting("x")
    outcome = await confirmed(inner, "x").resume("x", ARGS, _CONFIRM, True, AttemptContext(CHAT))
    assert outcome == ToolOutcome(True, "ran x")
    assert inner.calls == [("x", ARGS)]


async def test_resume_false_declines() -> None:
    inner = Counting("x")
    outcome = await confirmed(inner, "x").resume("x", ARGS, _CONFIRM, False, AttemptContext(CHAT))
    assert outcome == ToolOutcome(False, _DECLINED)
    assert inner.calls == []


async def test_other_states_go_to_the_inner_resume() -> None:
    inner = Asking("x")
    outcome = await confirmed(inner, "x").resume(
        "x", ARGS, {"via": "mcp"}, {"action": "accept"}, AttemptContext(CHAT)
    )
    assert outcome.text == "resumed x with {'action': 'accept'}"
    assert inner.calls == []


async def test_confirm_list_never_offers_a_tool() -> None:
    inner = Counting("mcparr_find_media", "mcparr_add_media")
    toolset = confirmed(inner, "mcparr_*", tools="mcparr_find_media")
    assert [d["function"]["name"] for d in toolset.definitions] == ["mcparr_find_media"]
    outcome = await dispatch([toolset], "mcparr_add_media", ARGS, AttemptContext(CHAT))
    assert outcome == ToolOutcome(False, "Error: unknown tool 'mcparr_add_media'.")


async def test_another_users_confirm_list_doesnt_apply() -> None:
    inner = Counting("mcparr_add_media")
    outcome = await dispatch([confirmed(inner)], "mcparr_add_media", ARGS, AttemptContext(CHAT))
    assert outcome == ToolOutcome(True, "ran mcparr_add_media")
