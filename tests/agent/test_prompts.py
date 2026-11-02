import re
from datetime import date

from alfred.agent.prompts import (
    NO_REPLY,
    OPERATING_RULES,
    butler_persona,
    is_no_reply,
    system_prompt,
    today_line,
)
from alfred.mcp.registry import ServerInstructions

COOKBOOK = ServerInstructions("cookbook", "Call list_units and use its codes.")
TODAY = date(2026, 11, 2)
# The operating rules as sent: the fixed text, then today's date.
RULES = OPERATING_RULES + "\n\nToday is Monday, 2 November 2026."


def make_prompt(persona: str | None, **kwargs: object) -> str:
    return system_prompt(persona, today=TODAY, **kwargs)  # type: ignore[arg-type]


def test_default_prompt_is_persona_then_rules() -> None:
    prompt = make_prompt(None, owner_address="sir", ask=False)
    assert prompt == butler_persona("sir") + "\n\n" + RULES
    assert 'Address the owner as "sir", at most once per reply' in prompt
    assert "butler" in prompt and "formal" in prompt
    assert "No preamble" in prompt and "no closing offers" in prompt
    assert "Telegram" in prompt
    assert "[notice:" in prompt and "[ask:" in prompt
    assert NO_REPLY not in prompt


def test_persona_covers_ask_interruptions() -> None:
    assert "Pardon the interruption, sir." in butler_persona("sir")


def test_custom_address_in_builtin_persona() -> None:
    prompt = make_prompt(None, owner_address="Mr. Ferreira", ask=False)
    assert 'Address the owner as "Mr. Ferreira"' in prompt
    assert '"sir"' not in prompt and ", sir." not in prompt


def test_custom_persona_replaces_only_the_butler() -> None:
    prompt = make_prompt("Be terse and casual.", owner_address="sir", ask=False)
    assert prompt == "Be terse and casual.\n\n" + RULES
    assert "butler" not in prompt and "sir" not in prompt
    assert "Telegram" in prompt
    assert "[notice:<service>] is a notification" in prompt and "[ask:" in prompt


def test_custom_persona_is_sent_as_written() -> None:
    persona = "Call me {boss} and use {} braces."
    prompt = make_prompt(persona, owner_address="sir", ask=False)
    assert prompt.startswith(persona + "\n\n")


def test_ask_turn_carries_no_reply_rule() -> None:
    assert NO_REPLY in make_prompt(None, owner_address="sir", ask=True)
    assert NO_REPLY in make_prompt("Be terse.", owner_address="sir", ask=True)


def test_unavailable_note() -> None:
    prompt = make_prompt(
        "Be terse.", owner_address="sir", ask=False, unavailable=["mcparr", "cookbook"]
    )
    assert "unavailable" in prompt and "mcparr, cookbook" in prompt
    assert prompt.index(RULES) < prompt.index("mcparr, cookbook")


def test_server_instructions_order_on_ask_turn() -> None:
    prompt = make_prompt(
        None, owner_address="sir", ask=True, unavailable=["mcparr"], instructions=[COOKBOOK]
    )
    persona = prompt.index(butler_persona("sir"))
    rules = prompt.index(RULES)
    cookbook = prompt.index(COOKBOOK.text)
    note = prompt.index("unavailable")
    no_reply = prompt.index(NO_REPLY)
    assert persona < rules < cookbook < note < no_reply


def test_server_instructions_framing() -> None:
    prompt = make_prompt(None, owner_address="sir", ask=False, instructions=[COOKBOOK])
    assert 'Instructions from the tool server "cookbook"' in prompt
    assert "offered to you as cookbook_<tool>" in prompt
    assert "use that prefixed name" in prompt


def test_one_block_per_server() -> None:
    other = ServerInstructions("trello", "Cards can't be archived.")
    prompt = make_prompt(None, owner_address="sir", ask=False, instructions=[COOKBOOK, other])
    assert prompt.index("cookbook_<tool>") < prompt.index(COOKBOOK.text)
    assert prompt.index(COOKBOOK.text) < prompt.index("trello_<tool>") < prompt.index(other.text)


def test_custom_persona_keeps_server_instructions() -> None:
    prompt = make_prompt("Be terse.", owner_address="sir", ask=False, instructions=[COOKBOOK])
    assert prompt.startswith("Be terse.\n\n" + RULES)
    assert COOKBOOK.text in prompt


def test_no_section_without_instructions() -> None:
    prompt = make_prompt(None, owner_address="sir", ask=False, instructions=[])
    assert prompt == butler_persona("sir") + "\n\n" + RULES
    assert "tool server" not in prompt


def test_is_no_reply() -> None:
    assert is_no_reply("  NO_REPLY\n")
    assert not is_no_reply("NO_REPLY, nothing to see")


# --- recalled conversations ------------------------------------------------------


def test_recall_rule_with_default_persona() -> None:
    text = make_prompt(None, owner_address="sir", ask=False)
    assert '[recall:#<id> "<title>"]' in text
    assert "brought back into this one at the owner's request" in text
    assert "did not just say it" in text


def test_recall_rule_with_custom_persona() -> None:
    text = make_prompt("Be terse.", owner_address="sir", ask=False)
    assert '[recall:#<id> "<title>"]' in text and "did not just say it" in text


# --- today's date ----------------------------------------------------------------


def test_today_line() -> None:
    assert today_line(date(2026, 11, 2)) == "Today is Monday, 2 November 2026."
    assert today_line(date(2026, 11, 3)) == "Today is Tuesday, 3 November 2026."


def test_date_follows_the_rules() -> None:
    text = make_prompt(None, owner_address="sir", ask=False, instructions=[COOKBOOK])
    assert text.index(OPERATING_RULES) < text.index("Today is Monday, 2 November 2026.")
    assert text.index("Today is Monday") < text.index(COOKBOOK.text)


def test_no_time_of_day() -> None:
    text = make_prompt(None, owner_address="sir", ask=False)
    assert not re.search(r"\d{1,2}:\d{2}", text)


def test_date_with_custom_persona_and_ask() -> None:
    assert "Today is Monday, 2 November 2026." in make_prompt(
        "Be terse.", owner_address="s", ask=True
    )
