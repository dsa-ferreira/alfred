"""The fact extraction request: its transcript, prompt and reply.

Only the user's own messages and alfred's direct replies reach
extraction: notices, ``/ask`` prompts and replies, reminders and their
replies, and recalled conversations are left out, so text from services
or other users never becomes a fact about this user.

The reply is one labelled line per fact::

    NEW: works night shifts on weekends
    REPLACE 7 "lives in Lisbon": lives in Porto
    NONE

A ``REPLACE`` quotes the fact it replaces, so a wrong id can be told from a
right one when it is applied.

read leniently, like the summary reply.

The duplicate check asks which of a user's close facts say the same
thing as a new one, answered with ``SAME <id>`` lines or ``NONE``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Protocol

from alfred.memory.transcript import TranscriptEntry, clean

# The entry kinds extraction reads.
_EXTRACTION_KINDS = ("user", "assistant")

_EXTRACTION_PROMPT = """\
You keep a short list of lasting facts about {user}, who chats with alfred, \
their assistant. Read the finished chat and find what is worth knowing about \
{user} in every future chat: stable preferences, identity, goals, ongoing \
projects, constraints (such as allergies or diets) and relationships with \
people.
Keep a fact only if knowing it would change how alfred helps {user} in a \
later chat about something else. These are not facts:
- instructions to alfred and their settings: setting, changing or \
cancelling reminders and briefings (even when they describe a schedule, \
such as a weekday briefing at 06:45), adding or editing recipes, and what \
to look up. alfred keeps those in its reminders and tools already;
- how {user} refers to people or things, unless the relationship itself is \
said ("Teach is her husband" is a fact; "refers to someone as Teach" is not);
- what was asked, discussed or done in this chat, one-off tasks and \
requests, small talk, and anything alfred says about itself.
Take facts only from what {user} says about themselves. alfred's replies are \
there only to make {user}'s messages clear: never take a fact from alfred's \
words alone, or from text {user} only quoted or forwarded.
Write each fact as one short sentence in the third person about {user}, such \
as "{user} is allergic to shellfish."
Don't repeat a fact that is already known. Replace a learned fact only when \
the new fact makes it untrue, never one about something else, and copy its \
text exactly. Told facts can't be replaced.

Answer with one line per fact and nothing else:
NEW: <a new fact>
REPLACE <id> "<the text of learned fact <id>, copied exactly>": <the new fact>
If there is nothing worth keeping, which is usual, answer with the single line:
NONE"""


# Bullets, numbering, quotes and Markdown emphasis before a label.
_PREFIX = r"^\s*(?:(?:[-*+•>]|\d+[.)])\s+)*[*_`#\s]*"
_AFTER_LABEL = r"[*_`]*\s*:[*_`\s]*(.*)$"
_NEW = re.compile(_PREFIX + r"new" + _AFTER_LABEL, re.IGNORECASE)
# ``REPLACE 7 "old": new``; the colon may be left out after a quote, and an
# old-style ``REPLACE 7: new`` has no quote.
_REPLACE = re.compile(
    _PREFIX
    + r"replace[*_`\s]*\[?#?(\d+)\]?[*_`]*\s*"
    + r"(?:[\"“”](.*?)[\"“”][*_`]*\s*:?|:)[*_`\s]*(.*)$",
    re.IGNORECASE,
)
_NONE = re.compile(_PREFIX + r"none[*_`.!\s]*$", re.IGNORECASE)


# How close a REPLACE quote must be to the fact's text.
_QUOTE_MIN_RATIO = 0.85
# A quote found inside the fact must cover at least this share of it.
_QUOTE_MIN_SHARE = 0.5
_NOT_WORD = re.compile(r"[\W_]+")


_DUPLICATE_PROMPT = """\
You check a list of short facts about {user} for repeats. Below are a new \
fact and some known facts. Which known facts say the same thing as the new \
fact, so that keeping both would only repeat it? A fact on the same topic, \
one that adds detail the other lacks, or one that contradicts it is not the \
same.

Answer with one line per known fact that is the same, and nothing else:
SAME <id>
If none is the same, answer with the single line:
NONE"""


_SAME = re.compile(_PREFIX + r"same[*_`:\s]*\[?#?(\d+)\]?[*_`.\s]*$", re.IGNORECASE)


def extraction_entries(entries: Sequence[TranscriptEntry]) -> list[TranscriptEntry]:
    """The entries extraction may read: the user's messages and alfred's replies to them."""
    return [e for e in entries if e.kind in _EXTRACTION_KINDS]


def extraction_messages(
    user: str, facts: Sequence[_KnownFact], transcript: str
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _EXTRACTION_PROMPT.format(user=user)},
        {"role": "user", "content": f"{_known_facts(user, facts)}\n\nThe chat:\n{transcript}"},
    ]


@dataclass(frozen=True)
class NewFact:
    text: str
    # The id of the learned fact it replaces, if the model said so.
    replaces: int | None = None
    # The text the model quoted for that fact; None when it quoted nothing.
    quote: str | None = None


class ExtractionParseError(ValueError):
    pass


def parse_facts(reply: str) -> list[NewFact]:
    """Read ``NEW:``, ``REPLACE <id> "<old>":`` and ``NONE`` lines, leniently.

    Unlabelled lines (prose, code fences) are ignored. ``NONE`` alone is an
    empty list. A reply with none of the labels is unusable.
    """
    found: list[NewFact] = []
    labelled = False
    for raw in reply.splitlines():
        if match := _REPLACE.match(raw):
            labelled = True
            text = clean(match.group(3))
            quote = match.group(2)
            if text:
                found.append(
                    NewFact(text, int(match.group(1)), quote.strip() if quote is not None else None)
                )
        elif match := _NEW.match(raw):
            labelled = True
            text = clean(match.group(1))
            if text:
                found.append(NewFact(text))
        elif _NONE.match(raw):
            labelled = True
    if not labelled:
        raise ExtractionParseError("no NEW:, REPLACE or NONE line in the model's reply")
    return found


def quote_matches(quote: str | None, fact_text: str, user: str) -> bool:
    """Whether a ``REPLACE`` quote names this fact.

    After normalising both: equal, a ``difflib`` ratio of at least
    ``QUOTE_MIN_RATIO``, or the quote's words found together in the fact and
    covering at least ``QUOTE_MIN_SHARE`` of it ("lives in Lisbon" for "lives
    in Lisbon, Portugal"). No quote never matches.
    """
    if quote is None:
        return False
    said, actual = _normalised(quote, user), _normalised(fact_text, user)
    if not said or not actual:
        return False
    if said == actual or SequenceMatcher(None, said, actual).ratio() >= _QUOTE_MIN_RATIO:
        return True
    return f" {said} " in f" {actual} " and len(said) >= _QUOTE_MIN_SHARE * len(actual)


def duplicate_messages(
    user: str, new_fact: str, candidates: Sequence[_KnownFact]
) -> list[dict[str, str]]:
    """The duplicate-check request. Origins are left out: alfred decides which stays."""
    known = "\n".join(f"[{f.id}] {f.text}" for f in candidates)
    return [
        {"role": "system", "content": _DUPLICATE_PROMPT.format(user=user)},
        {"role": "user", "content": f"New fact: {new_fact}\n\nKnown facts:\n{known}"},
    ]


class DuplicateParseError(ValueError):
    pass


def parse_duplicates(reply: str) -> list[int]:
    """The ids on ``SAME <id>`` lines, in order; ``NONE`` alone is an empty list.

    Unlabelled lines are ignored; a reply with neither label is unusable.
    """
    found: list[int] = []
    labelled = False
    for raw in reply.splitlines():
        if match := _SAME.match(raw):
            labelled = True
            if (fact_id := int(match.group(1))) not in found:
                found.append(fact_id)
        elif _NONE.match(raw):
            labelled = True
    if not labelled:
        raise DuplicateParseError("no SAME or NONE line in the model's reply")
    return found


class _KnownFact(Protocol):
    id: int
    text: str
    origin: str


def _known_facts(user: str, facts: Sequence[_KnownFact]) -> str:
    if not facts:
        return f"Known facts about {user}: none."
    lines = [f"Known facts about {user}:"]
    for fact in facts:
        label = "told, can't be replaced" if fact.origin == "told" else "learned"
        lines.append(f"[{fact.id}] ({label}) {fact.text}")
    return "\n".join(lines)


def _normalised(text: str, user: str) -> str:
    """Case-folded words only, without the user's name at the start."""
    words = _NOT_WORD.sub(" ", text.casefold()).split()
    if words and words[0] == user.casefold():
        words = words[1:]
    return " ".join(words)
