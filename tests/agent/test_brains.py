from __future__ import annotations

import json

from alfred.agent.brains import SET_MODEL, Brains, BrainTools
from alfred.agent.toolset import AttemptContext, ToolOutcome
from alfred.agent.turn import TurnInput

GEMMA = "gemma4:26b-a4b-it-qat"


def test_startup_model_has_an_alias() -> None:
    brains = Brains(GEMMA, {"gemma": GEMMA, "bonsai": "bonsai:8b"})
    assert brains.names == ["gemma", "bonsai"]
    assert brains.current.name == "gemma" and brains.tag == GEMMA
    assert brains.is_default


def test_startup_model_without_an_alias() -> None:
    brains = Brains("qwen3:14b", {"bonsai": "bonsai:8b"})
    assert brains.names == ["qwen3:14b", "bonsai"]
    assert brains.current.name == "qwen3:14b" and brains.tag == "qwen3:14b"


def test_no_list() -> None:
    brains = Brains(GEMMA)
    assert brains.names == [GEMMA]
    assert brains.others == []


def test_set_switches() -> None:
    brains = Brains(GEMMA, {"gemma": GEMMA, "bonsai": "bonsai:8b"})
    assert brains.set("bonsai") == "switched"
    assert brains.current.name == "bonsai" and brains.tag == "bonsai:8b"
    assert not brains.is_default
    assert brains.others == ["gemma"]


def test_set_unknown_keeps_current() -> None:
    brains = Brains(GEMMA, {"gemma": GEMMA, "bonsai": "bonsai:8b"})
    assert brains.set("claude") == "unknown"
    assert brains.current.name == "gemma"


def test_set_already_current() -> None:
    brains = Brains(GEMMA, {"gemma": GEMMA, "bonsai": "bonsai:8b"})
    assert brains.set("gemma") == "already"
    assert brains.current.name == "gemma"


def test_fall_back_from_another_brain() -> None:
    brains = Brains(GEMMA, {"gemma": GEMMA, "bonsai": "bonsai:8b"})
    brains.set("bonsai")
    assert brains.fall_back() is True
    assert brains.current.name == "gemma"


def test_fall_back_on_the_default_does_nothing() -> None:
    brains = Brains(GEMMA, {"gemma": GEMMA, "bonsai": "bonsai:8b"})
    assert brains.fall_back() is False
    assert brains.current.name == "gemma"


# --- alfred_set_model ---------------------------------------------------------------

OWNER = TurnInput("owner", "use bonsai", 1)
TWO = {"gemma": "gemma4", "bonsai": "bonsai:8b"}


async def test_set_model_offered_on_owner_turns_with_two_brains() -> None:
    tools = BrainTools(Brains("gemma4", TWO))
    assert await tools.open(OWNER) is tools
    assert await tools.open(TurnInput("ask", "x", 2, "svc")) is None
    assert await BrainTools(Brains("gemma4")).open(OWNER) is None
    [definition] = tools.definitions
    assert definition["function"]["name"] == SET_MODEL
    assert definition["function"]["parameters"]["properties"]["model"]["enum"] == [
        "gemma",
        "bonsai",
    ]


async def test_set_model_switches() -> None:
    brains = Brains("gemma4", TWO)
    outcome = await BrainTools(brains).call(
        SET_MODEL, json.dumps({"model": "bonsai"}), AttemptContext(OWNER)
    )
    assert outcome.ok and brains.current.name == "bonsai"
    assert outcome.text.startswith('Switched to the brain "bonsai" (bonsai:8b).')


async def test_set_model_unknown_and_already() -> None:
    brains = Brains("gemma4", TWO)
    tools = BrainTools(brains)
    ctx = AttemptContext(OWNER)
    assert await tools.call(SET_MODEL, '{"model": "x"}', ctx) == ToolOutcome(
        False, "Error: unknown brain 'x'. The brains are: gemma, bonsai."
    )
    assert await tools.call(SET_MODEL, '{"model": "gemma"}', ctx) == ToolOutcome(
        True, 'The brain "gemma" is already in use; nothing changed.'
    )
    assert brains.current.name == "gemma"
