from alfred.agent.prompts import (
    NO_REPLY,
    OPERATING_RULES,
    butler_persona,
    is_no_reply,
    system_prompt,
)


def test_default_prompt_is_persona_then_rules() -> None:
    prompt = system_prompt(None, owner_address="sir", ask=False)
    assert prompt == butler_persona("sir") + "\n\n" + OPERATING_RULES
    assert 'Address the owner as "sir", at most once per reply' in prompt
    assert "butler" in prompt and "formal" in prompt
    assert "No preamble" in prompt and "no closing offers" in prompt
    assert "Telegram" in prompt
    assert "[notice:" in prompt and "[ask:" in prompt
    assert NO_REPLY not in prompt


def test_persona_covers_ask_interruptions() -> None:
    assert "Pardon the interruption, sir." in butler_persona("sir")


def test_custom_address_in_builtin_persona() -> None:
    prompt = system_prompt(None, owner_address="Mr. Ferreira", ask=False)
    assert 'Address the owner as "Mr. Ferreira"' in prompt
    assert '"sir"' not in prompt and ", sir." not in prompt


def test_custom_persona_replaces_only_the_butler() -> None:
    prompt = system_prompt("Be terse and casual.", owner_address="sir", ask=False)
    assert prompt == "Be terse and casual.\n\n" + OPERATING_RULES
    assert "butler" not in prompt and "sir" not in prompt
    assert "Telegram" in prompt
    assert "[notice:<service>] is a notification" in prompt and "[ask:" in prompt


def test_custom_persona_is_sent_as_written() -> None:
    persona = "Call me {boss} and use {} braces."
    prompt = system_prompt(persona, owner_address="sir", ask=False)
    assert prompt.startswith(persona + "\n\n")


def test_ask_turn_carries_no_reply_rule() -> None:
    assert NO_REPLY in system_prompt(None, owner_address="sir", ask=True)
    assert NO_REPLY in system_prompt("Be terse.", owner_address="sir", ask=True)


def test_unavailable_note() -> None:
    prompt = system_prompt(
        "Be terse.", owner_address="sir", ask=False, unavailable=["mcparr", "cookbook"]
    )
    assert "unavailable" in prompt and "mcparr, cookbook" in prompt
    assert prompt.index(OPERATING_RULES) < prompt.index("mcparr, cookbook")


def test_is_no_reply() -> None:
    assert is_no_reply("  NO_REPLY\n")
    assert not is_no_reply("NO_REPLY, nothing to see")
