"""The duplicate check's request and reply parser."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from alfred.features.facts.duplicate_prompt import (
    DuplicateParseError,
    duplicate_messages,
    parse_duplicates,
)


@dataclass(frozen=True)
class Known:
    id: int
    text: str
    origin: str


# --- The duplicate check (change fix-fact-removals, design D3) -----------------------------


def test_duplicate_request_lists_candidates_without_origins() -> None:
    candidates = [Known(12, "ana likes Teach very much.", "told")]
    system, user = duplicate_messages("ana", "ana has a person she refers to as Teach.", candidates)
    assert "ana" in system["content"] and "SAME <id>" in system["content"]
    assert "contradicts" in system["content"] and "adds detail" in system["content"]
    assert user["content"] == (
        "New fact: ana has a person she refers to as Teach.\n\n"
        "Known facts:\n[12] ana likes Teach very much."
    )
    assert "told" not in user["content"]


@pytest.mark.parametrize(
    "reply",
    [
        "SAME 12\nSAME 15",
        "- SAME 12\n- SAME 15",
        "1. SAME 12\n2. SAME 15",
        "**SAME** 12\n**SAME 15**",
        "```\nSAME [12]\nSAME #15\n```",
        "The same ones:\nsame: 12\nSame 15.\nSAME 12",
    ],
)
def test_parse_duplicates_is_lenient(reply: str) -> None:
    assert parse_duplicates(reply) == [12, 15]


@pytest.mark.parametrize("reply", ["NONE", "**NONE**", "- none.", "```\nNONE\n```"])
def test_duplicates_none_is_empty(reply: str) -> None:
    assert parse_duplicates(reply) == []


@pytest.mark.parametrize("reply", ["", "They are all different.", "NEW: something", "SAME"])
def test_duplicate_reply_without_labels_is_an_error(reply: str) -> None:
    with pytest.raises(DuplicateParseError):
        parse_duplicates(reply)
