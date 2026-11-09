from __future__ import annotations

from alfred.agent.brains import Brains

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
