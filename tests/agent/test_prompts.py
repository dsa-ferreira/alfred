from alfred.agent.prompts import DEFAULT_SYSTEM_PROMPT, NO_REPLY, is_no_reply, system_prompt


def test_default_prompt() -> None:
    prompt = system_prompt(None, ask=False)
    assert prompt == DEFAULT_SYSTEM_PROMPT
    assert "Telegram" in prompt and "short" in prompt
    assert "[notice:" in prompt and "[ask:" in prompt
    assert NO_REPLY not in prompt


def test_custom_prompt_replaces_default() -> None:
    prompt = system_prompt("Be terse.", ask=False)
    assert prompt == "Be terse."


def test_ask_turn_carries_no_reply_rule() -> None:
    assert NO_REPLY in system_prompt(None, ask=True)
    assert NO_REPLY in system_prompt("Be terse.", ask=True)


def test_unavailable_note() -> None:
    prompt = system_prompt(None, ask=False, unavailable=["mcparr", "cookbook"])
    assert "unavailable" in prompt and "mcparr, cookbook" in prompt


def test_is_no_reply() -> None:
    assert is_no_reply("  NO_REPLY\n")
    assert not is_no_reply("NO_REPLY, nothing to see")
