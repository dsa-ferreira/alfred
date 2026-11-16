"""The duplicate check request: which of a user's close facts say the same
thing as a new one, answered with ``SAME <id>`` lines or ``NONE``, read
leniently like the extraction reply.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Protocol

from alfred.facts.extraction_prompt import LINE_PREFIX, NONE_LINE

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


_SAME = re.compile(LINE_PREFIX + r"same[*_`:\s]*\[?#?(\d+)\]?[*_`.\s]*$", re.IGNORECASE)


def duplicate_messages(
    user: str, new_fact: str, candidates: Sequence[_Candidate]
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
        elif NONE_LINE.match(raw):
            labelled = True
    if not labelled:
        raise DuplicateParseError("no SAME or NONE line in the model's reply")
    return found


class _Candidate(Protocol):
    id: int
    text: str
