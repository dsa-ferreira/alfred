"""The summary prompt, and reading the model's title and summary back."""

from __future__ import annotations

import re
from dataclasses import dataclass

from alfred.conversations.transcript import clean

_SUMMARY_PROMPT = """\
You title and summarise a finished chat between a user and alfred, their \
assistant, so it can be found again later.
Answer with exactly two lines and nothing else:
Title: <the topic in at most six words>
Summary: <one or two sentences on what was discussed and how it turned out. Name \
the concrete outcomes: names, numbers, decisions, what was added or changed.>"""

_TITLE_MAX_CHARS = 80
_FALLBACK_TITLE_WORDS = 5


_LABEL = re.compile(r"^[\s*_#>`-]*(title|summary)[\s*_`]*:(.*)$", re.IGNORECASE)


def summary_messages(transcript: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _SUMMARY_PROMPT},
        {"role": "user", "content": transcript},
    ]


class SummaryParseError(ValueError):
    pass


def parse_summary(reply: str) -> _Summary:
    """Read ``Title:`` and ``Summary:`` lines, leniently.

    Any case, Markdown bold or headings, and quotes are accepted. Lines after
    ``Summary:`` that carry no label continue the summary. Without a title,
    the summary's first five words become one. Without a summary, the reply
    is unusable.
    """
    title: str | None = None
    summary_lines: list[str] | None = None
    for raw in reply.splitlines():
        match = _LABEL.match(raw)
        if match:
            label, value = match.group(1).lower(), clean(match.group(2))
            if label == "title" and title is None:
                title = value
            elif label == "summary" and summary_lines is None:
                summary_lines = [value] if value else []
            continue
        if summary_lines is not None and raw.strip():
            summary_lines.append(clean(raw))
    summary = " ".join(s for s in (summary_lines or []) if s).strip()
    if not summary:
        raise SummaryParseError("no Summary: line in the model's reply")
    if not title:
        words = summary.split()[:_FALLBACK_TITLE_WORDS]
        title = " ".join(words).rstrip(".,;:!?")
    return _Summary(title[:_TITLE_MAX_CHARS].strip(), summary)


@dataclass(frozen=True)
class _Summary:
    title: str
    summary: str
