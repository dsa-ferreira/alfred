"""Transcripts of archived conversations, and the summary prompt (memory design D5).

A transcript is one line per entry: ``Owner:``, ``alfred:``,
``[notice:<service>]``, ``[ask:<service>]``, ``[reminder:#<id>]`` and, for a recall, only
``[recalled #<id> "<title>"]``, so a recalled conversation is never summarised
or recalled again as part of the conversation it was brought into.

An owner line ends with the handles of its attachments (change add-attachments,
design D11), so a recalled conversation's files can be opened again; never
image data or PDF text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from alfred.attachments.handles import AttachmentRef, handle, with_handles

SUMMARY_PROMPT = """\
You title and summarise a finished chat between the owner and alfred, their \
assistant, so it can be found again later.
Answer with exactly two lines and nothing else:
Title: <the topic in at most six words>
Summary: <one or two sentences on what was discussed and how it turned out. Name \
the concrete outcomes: names, numbers, decisions, what was added or changed.>"""

TITLE_MAX_CHARS = 80
_FALLBACK_TITLE_WORDS = 5


@dataclass(frozen=True)
class TranscriptEntry:
    kind: str
    source: str | None
    text: str
    recalled_id: int | None = None
    recalled_title: str | None = None
    reminder_id: int | None = None
    entry_id: int | None = None
    attachments: tuple[AttachmentRef, ...] = ()


def render(entries: list[TranscriptEntry]) -> str:
    """The conversation as plain text, one entry per line (multi-line entries kept)."""
    return "\n".join(_line(e) for e in entries)


def _line(e: TranscriptEntry) -> str:
    match e.kind:
        case "owner":
            return f"Owner: {with_handles(e.text, (handle(a) for a in e.attachments))}"
        case "assistant" | "ask_reply" | "reminder_reply":
            return f"alfred: {e.text}"
        case "reminder":
            return f"[reminder:#{e.reminder_id}] {e.text}"
        case "notice":
            return f"[notice:{e.source}] {e.text}"
        case "ask_prompt":
            return f"[ask:{e.source}] {e.text}"
        case "recall":
            return f"[recalled {recall_label(e.recalled_id, e.recalled_title)}]"
        case _:
            return e.text


def recall_label(conversation_id: int | None, title: str | None) -> str:
    """``#41 "Chicken thighs recipe"``, or ``#41`` when there is no title yet."""
    return f'#{conversation_id} "{title}"' if title else f"#{conversation_id}"


def cut_middle(text: str, max_chars: int) -> str:
    """At most ``max_chars``: the start and the end, with the omission marked.

    The start of a conversation usually says what it is about, the end how it
    turned out.
    """
    if len(text) <= max_chars:
        return text
    room = len(_omitted(len(text)))
    keep = max_chars - room
    if keep <= 0:
        return text[:max_chars]
    head = keep // 2
    tail = keep - head
    return text[:head] + _omitted(len(text) - keep) + text[len(text) - tail :]


def _omitted(n: int) -> str:
    return f"\n[… {n} characters omitted …]\n"


def summary_messages(transcript: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SUMMARY_PROMPT},
        {"role": "user", "content": transcript},
    ]


@dataclass(frozen=True)
class Summary:
    title: str
    summary: str


class SummaryParseError(ValueError):
    pass


_LABEL = re.compile(r"^[\s*_#>`-]*(title|summary)[\s*_`]*:(.*)$", re.IGNORECASE)
_QUOTES = "\"'“”‘’`"


def parse_summary(reply: str) -> Summary:
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
            label, value = match.group(1).lower(), _clean(match.group(2))
            if label == "title" and title is None:
                title = value
            elif label == "summary" and summary_lines is None:
                summary_lines = [value] if value else []
            continue
        if summary_lines is not None and raw.strip():
            summary_lines.append(_clean(raw))
    summary = " ".join(s for s in (summary_lines or []) if s).strip()
    if not summary:
        raise SummaryParseError("no Summary: line in the model's reply")
    if not title:
        words = summary.split()[:_FALLBACK_TITLE_WORDS]
        title = " ".join(words).rstrip(".,;:!?")
    return Summary(title[:TITLE_MAX_CHARS].strip(), summary)


def _clean(value: str) -> str:
    value = value.strip().strip("*_").strip()
    if len(value) >= 2 and value[0] in _QUOTES and value[-1] in _QUOTES:
        value = value[1:-1].strip()
    return value


def local_date(moment: datetime, zone: ZoneInfo) -> str:
    """``Tue 3 Nov 2026``, in the owner's timezone."""
    day = moment.astimezone(zone)
    return f"{day:%a} {day.day} {day:%b %Y}"


def date_span(started: datetime, ended: datetime | None, zone: ZoneInfo) -> str:
    start = local_date(started, zone)
    end = local_date(ended, zone) if ended is not None else "now"
    return f"{start} to {end}"
