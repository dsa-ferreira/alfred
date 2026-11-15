"""The fact extraction request: its transcript, prompt and reply (change add-user-facts).

Only the user's own messages and alfred's direct replies reach extraction
(design D4): notices, ``/ask`` prompts and replies, reminders and their
replies, and recalled conversations are left out, so text from services or
other users never becomes a fact about this user.

The reply is one labelled line per fact (design D5)::

    NEW: works night shifts on weekends
    REPLACE 7: lives in Porto
    NONE

read leniently, like the summary reply.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from alfred.memory.transcript import TranscriptEntry, _clean

# The entry kinds extraction reads.
EXTRACTION_KINDS = ("user", "assistant")

EXTRACTION_PROMPT = """\
You keep a short list of lasting facts about {user}, who chats with alfred, \
their assistant. Read the finished chat and find what is worth knowing about \
{user} in every future chat: stable preferences, identity, goals, ongoing \
projects, constraints (such as allergies or diets) and relationships with \
people.
Leave out one-off tasks and requests, small talk, anything only looked up or \
done once, and anything alfred says about itself.
Take facts only from what {user} says about themselves. alfred's replies are \
there only to make {user}'s messages clear: never take a fact from alfred's \
words alone, or from text {user} only quoted or forwarded.
Write each fact as one short sentence in the third person about {user}, such \
as "{user} is allergic to shellfish."
Don't repeat a fact that is already known. When a new fact makes a learned \
fact out of date, replace that fact. Told facts can't be replaced.

Answer with one line per fact and nothing else:
NEW: <a new fact>
REPLACE <id>: <the new fact that replaces learned fact <id>>
If there is nothing worth keeping, which is usual, answer with the single line:
NONE"""


class KnownFact(Protocol):
    id: int
    text: str
    origin: str


def extraction_entries(entries: Sequence[TranscriptEntry]) -> list[TranscriptEntry]:
    """The entries extraction may read: the user's messages and alfred's replies to them."""
    return [e for e in entries if e.kind in EXTRACTION_KINDS]


def known_facts(user: str, facts: Sequence[KnownFact]) -> str:
    if not facts:
        return f"Known facts about {user}: none."
    lines = [f"Known facts about {user}:"]
    for fact in facts:
        label = "told, can't be replaced" if fact.origin == "told" else "learned"
        lines.append(f"[{fact.id}] ({label}) {fact.text}")
    return "\n".join(lines)


def extraction_messages(
    user: str, facts: Sequence[KnownFact], transcript: str
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": EXTRACTION_PROMPT.format(user=user)},
        {"role": "user", "content": f"{known_facts(user, facts)}\n\nThe chat:\n{transcript}"},
    ]


@dataclass(frozen=True)
class NewFact:
    text: str
    # The id of the learned fact it replaces, if the model said so.
    replaces: int | None = None


class ExtractionParseError(ValueError):
    pass


# Bullets, numbering, quotes and Markdown emphasis before a label.
_PREFIX = r"^\s*(?:(?:[-*+•>]|\d+[.)])\s+)*[*_`#\s]*"
_AFTER_LABEL = r"[*_`]*\s*:[*_`\s]*(.*)$"
_NEW = re.compile(_PREFIX + r"new" + _AFTER_LABEL, re.IGNORECASE)
_REPLACE = re.compile(_PREFIX + r"replace[*_`\s]*\[?#?(\d+)\]?" + _AFTER_LABEL, re.IGNORECASE)
_NONE = re.compile(_PREFIX + r"none[*_`.!\s]*$", re.IGNORECASE)


def parse_facts(reply: str) -> list[NewFact]:
    """Read ``NEW:``, ``REPLACE <id>:`` and ``NONE`` lines, leniently.

    Unlabelled lines (prose, code fences) are ignored. ``NONE`` alone is an
    empty list. A reply with none of the labels is unusable.
    """
    found: list[NewFact] = []
    labelled = False
    for raw in reply.splitlines():
        if match := _REPLACE.match(raw):
            labelled = True
            text = _clean(match.group(2))
            if text:
                found.append(NewFact(text, int(match.group(1))))
        elif match := _NEW.match(raw):
            labelled = True
            text = _clean(match.group(1))
            if text:
                found.append(NewFact(text))
        elif _NONE.match(raw):
            labelled = True
    if not labelled:
        raise ExtractionParseError("no NEW:, REPLACE or NONE line in the model's reply")
    return found
