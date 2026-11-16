from __future__ import annotations

import pytest

from alfred.archive.prompts import (
    _SUMMARY_PROMPT,
    SummaryParseError,
    _Summary,
    parse_summary,
    summary_messages,
)

# --- summary prompt and parsing --------------------------------------------------


def test_summary_messages() -> None:
    messages = summary_messages("User: hi")
    assert messages == [
        {"role": "system", "content": _SUMMARY_PROMPT},
        {"role": "user", "content": "User: hi"},
    ]
    assert "Title:" in _SUMMARY_PROMPT and "Summary:" in _SUMMARY_PROMPT


def test_parse_plain() -> None:
    reply = "Title: Chicken thighs recipe\nSummary: A lemon-garlic recipe. Saved as recipe 12."
    assert parse_summary(reply) == _Summary(
        "Chicken thighs recipe", "A lemon-garlic recipe. Saved as recipe 12."
    )


@pytest.mark.parametrize(
    "reply",
    [
        "**Title:** Chicken thighs recipe\n**Summary:** Lemon and garlic.",
        '**Title**: "Chicken thighs recipe"\n**Summary**: Lemon and garlic.',
        "TITLE: 'Chicken thighs recipe'\nsummary:   Lemon and garlic.  ",
        "Sure! Here it is:\n\n  title :  “Chicken thighs recipe”\n\nSummary: Lemon and garlic.\n",
        "## Title: Chicken thighs recipe\n- Summary: Lemon and garlic.",
    ],
)
def test_parse_leniently(reply: str) -> None:
    assert parse_summary(reply) == _Summary("Chicken thighs recipe", "Lemon and garlic.")


def test_summary_continues_on_following_lines() -> None:
    reply = "Title: Fix\nSummary: The disk filled up.\nDownloads were moved to /mnt/big."
    assert parse_summary(reply).summary == "The disk filled up. Downloads were moved to /mnt/big."


def test_missing_title_uses_first_five_words() -> None:
    reply = "Summary: Lemon garlic chicken thighs, roasted at 200°C for 40 minutes."
    assert parse_summary(reply).title == "Lemon garlic chicken thighs, roasted"


def test_missing_title_short_summary() -> None:
    assert parse_summary("Summary: Disk cleaned.").title == "Disk cleaned"


@pytest.mark.parametrize(
    "reply", ["", "Here is a nice summary of your chat.", "Title: Only a title", "Summary:"]
)
def test_unusable_reply(reply: str) -> None:
    with pytest.raises(SummaryParseError):
        parse_summary(reply)


def test_long_title_is_cut() -> None:
    parsed = parse_summary("Title: " + "word " * 40 + "\nSummary: s")
    assert len(parsed.title) <= 80
