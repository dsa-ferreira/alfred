from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from alfred.conversations.transcript import (
    TranscriptEntry,
    _local_date,
    cut_middle,
    date_span,
    render,
)

LISBON = ZoneInfo("Europe/Lisbon")

# --- rendering -------------------------------------------------------------------


def test_render_every_kind() -> None:
    entries = [
        TranscriptEntry("user", None, "chicken thighs tonight?"),
        TranscriptEntry("assistant", None, "Lemon and garlic, sir."),
        TranscriptEntry("notice", "backup", "Nightly backup done"),
        TranscriptEntry("ask_prompt", "disk-check", "93% full"),
        TranscriptEntry("ask_reply", "disk-check", "The disk is filling up."),
        TranscriptEntry("recall", None, "#41 ... long transcript ...", 41, "Chicken thighs recipe"),
    ]
    assert render(entries) == "\n".join(
        [
            "User: chicken thighs tonight?",
            "alfred: Lemon and garlic, sir.",
            "[notice:backup] Nightly backup done",
            "[ask:disk-check] 93% full",
            "alfred: The disk is filling up.",
            '[recalled #41 "Chicken thighs recipe"]',
        ]
    )


def test_recall_is_rendered_by_title_only() -> None:
    entry = TranscriptEntry("recall", None, "User: secret detail from #41", 41, "Old chat")
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


# --- dates -----------------------------------------------------------------------


def test_local_date_uses_the_timezone() -> None:
    # 23:30 UTC on 2 Nov is 23:30 in Lisbon (WET) but already 3 Nov in Tokyo.
    moment = datetime(2026, 11, 2, 23, 30, tzinfo=UTC)
    assert _local_date(moment, LISBON) == "Mon 2 Nov 2026"
    assert _local_date(moment, ZoneInfo("Asia/Tokyo")) == "Tue 3 Nov 2026"


def test_date_span() -> None:
    start = datetime(2026, 10, 3, 10, tzinfo=UTC)
    end = datetime(2026, 10, 3, 11, tzinfo=UTC)
    assert date_span(start, end, LISBON) == "Sat 3 Oct 2026 to Sat 3 Oct 2026"


def test_render_reminder() -> None:
    entries = [
        TranscriptEntry("reminder", None, "call mum", reminder_id=12),
        TranscriptEntry("reminder_reply", None, "Your call to mum is due, sir.", reminder_id=12),
    ]
    assert render(entries) == "[reminder:#12] call mum\nalfred: Your call to mum is due, sir."
