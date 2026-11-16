from __future__ import annotations

from alfred.turns.inputs.titles import reminder_label, title


def test_titles_say_what_asked() -> None:
    assert title("user") == "For your request:"
    assert title("ask", source="jellyfin") == "For a request from jellyfin:"
    label = reminder_label(12, "call mum")
    assert title("reminder", reminder=label) == "For reminder #12 (call mum):"
