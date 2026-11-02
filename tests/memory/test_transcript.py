from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from alfred.memory.transcript import (
    SUMMARY_PROMPT,
    Summary,
    SummaryParseError,
    TranscriptEntry,
    cut_middle,
    date_span,
    local_date,
    parse_summary,
    render,
    summary_messages,
)

LISBON = ZoneInfo("Europe/Lisbon")

# --- rendering -------------------------------------------------------------------


def test_render_every_kind() -> None:
    entries = [
        TranscriptEntry("owner", None, "chicken thighs tonight?"),
        TranscriptEntry("assistant", None, "Lemon and garlic, sir."),
        TranscriptEntry("notice", "backup", "Nightly backup done"),
        TranscriptEntry("ask_prompt", "disk-check", "93% full"),
        TranscriptEntry("ask_reply", "disk-check", "The disk is filling up."),
        TranscriptEntry("recall", None, "#41 ... long transcript ...", 41, "Chicken thighs recipe"),
    ]
    assert render(entries) == "\n".join(
        [
            "Owner: chicken thighs tonight?",
            "alfred: Lemon and garlic, sir.",
            "[notice:backup] Nightly backup done",
            "[ask:disk-check] 93% full",
            "alfred: The disk is filling up.",
            '[recalled #41 "Chicken thighs recipe"]',
        ]
    )


def test_recall_is_rendered_by_title_only() -> None:
    entry = TranscriptEntry("recall", None, "Owner: secret detail from #41", 41, "Old chat")
    assert "secret detail" not in render([entry])


def test_recall_without_title() -> None:
    assert render([TranscriptEntry("recall", None, "x", 41, None)]) == "[recalled #41]"


def test_render_empty() -> None:
    assert render([]) == ""


# --- the cut ---------------------------------------------------------------------


def test_short_text_is_kept() -> None:
    assert cut_middle("abc", 3) == "abc"


def test_cut_keeps_start_and_end_with_marker() -> None:
    text = "S" * 500 + "M" * 9000 + "E" * 500
    cut = cut_middle(text, 1000)
    assert len(cut) <= 1000
    assert cut.startswith("S" * 400)
    assert cut.endswith("E" * 400)
    marker = cut[cut.index("[") : cut.index("]") + 1]
    omitted = int(marker.split()[1])
    assert marker == f"[… {omitted} characters omitted …]"
    # What's kept plus what's omitted is the whole text.
    assert len(cut) - len(f"\n{marker}\n") + omitted == len(text)


def test_cut_with_tiny_budget() -> None:
    assert cut_middle("x" * 100, 5) == "xxxxx"


# --- summary prompt and parsing --------------------------------------------------


def test_summary_messages() -> None:
    messages = summary_messages("Owner: hi")
    assert messages == [
        {"role": "system", "content": SUMMARY_PROMPT},
        {"role": "user", "content": "Owner: hi"},
    ]
    assert "Title:" in SUMMARY_PROMPT and "Summary:" in SUMMARY_PROMPT


def test_parse_plain() -> None:
    reply = "Title: Chicken thighs recipe\nSummary: A lemon-garlic recipe. Saved as recipe 12."
    assert parse_summary(reply) == Summary(
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
    assert parse_summary(reply) == Summary("Chicken thighs recipe", "Lemon and garlic.")


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


# --- dates -----------------------------------------------------------------------


def test_local_date_uses_the_timezone() -> None:
    # 23:30 UTC on 2 Nov is 23:30 in Lisbon (WET) but already 3 Nov in Tokyo.
    moment = datetime(2026, 11, 2, 23, 30, tzinfo=UTC)
    assert local_date(moment, LISBON) == "Mon 2 Nov 2026"
    assert local_date(moment, ZoneInfo("Asia/Tokyo")) == "Tue 3 Nov 2026"


def test_date_span() -> None:
    start = datetime(2026, 10, 3, 10, tzinfo=UTC)
    end = datetime(2026, 10, 3, 11, tzinfo=UTC)
    assert date_span(start, end, LISBON) == "Sat 3 Oct 2026 to Sat 3 Oct 2026"
