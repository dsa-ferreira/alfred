from alfred.agent.prompts import (
    NO_REPLY,
    OPERATING_RULES,
    butler_persona,
    is_no_reply,
    system_prompt,
)
from alfred.mcp.registry import ServerInstructions

COOKBOOK = ServerInstructions("cookbook", "Call list_units and use its codes.")


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


def test_server_instructions_order_on_ask_turn() -> None:
    prompt = system_prompt(
        None, owner_address="sir", ask=True, unavailable=["mcparr"], instructions=[COOKBOOK]
    )
    persona = prompt.index(butler_persona("sir"))
    rules = prompt.index(OPERATING_RULES)
    cookbook = prompt.index(COOKBOOK.text)
    note = prompt.index("unavailable")
    no_reply = prompt.index(NO_REPLY)
    assert persona < rules < cookbook < note < no_reply


def test_server_instructions_framing() -> None:
    prompt = system_prompt(None, owner_address="sir", ask=False, instructions=[COOKBOOK])
    assert 'Instructions from the tool server "cookbook"' in prompt
    assert "offered to you as cookbook_<tool>" in prompt
    assert "use that prefixed name" in prompt


def test_one_block_per_server() -> None:
    other = ServerInstructions("trello", "Cards can't be archived.")
    prompt = system_prompt(None, owner_address="sir", ask=False, instructions=[COOKBOOK, other])
    assert prompt.index("cookbook_<tool>") < prompt.index(COOKBOOK.text)
    assert prompt.index(COOKBOOK.text) < prompt.index("trello_<tool>") < prompt.index(other.text)


def test_custom_persona_keeps_server_instructions() -> None:
    prompt = system_prompt("Be terse.", owner_address="sir", ask=False, instructions=[COOKBOOK])
    assert prompt.startswith("Be terse.\n\n" + OPERATING_RULES)
    assert COOKBOOK.text in prompt


def test_no_section_without_instructions() -> None:
    prompt = system_prompt(None, owner_address="sir", ask=False, instructions=[])
    assert prompt == butler_persona("sir") + "\n\n" + OPERATING_RULES
    assert "tool server" not in prompt


def test_is_no_reply() -> None:
    assert is_no_reply("  NO_REPLY\n")
    assert not is_no_reply("NO_REPLY, nothing to see")
