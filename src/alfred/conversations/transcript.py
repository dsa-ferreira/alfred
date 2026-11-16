"""Transcripts of conversations.

A transcript is one line per entry: ``User:``, ``alfred:``,
``[notice:<service>]``, ``[ask:<service>]``, ``[reminder:#<id>]`` and, for a recall, only
``[recalled #<id> "<title>"]``, so a recalled conversation is never summarised
or recalled again as part of the conversation it was brought into.

A user line ends with the handles of its attachments, so a recalled
conversation's files can be opened again; never image data or PDF text.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from alfred.attachments.handles import AttachmentRef, handle, refs_by_entry, with_handles
from alfred.db.models import Conversation, Entry

_QUOTES = "\"'“”‘’`"


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


def clean(value: str) -> str:
    value = value.strip().strip("*_").strip()
    if len(value) >= 2 and value[0] in _QUOTES and value[-1] in _QUOTES:
        value = value[1:-1].strip()
    return value


def date_span(started: datetime, ended: datetime | None, zone: ZoneInfo) -> str:
    start = _local_date(started, zone)
    end = _local_date(ended, zone) if ended is not None else "now"
    return f"{start} to {end}"


async def load_transcript(session: AsyncSession, conversation_id: int) -> list[TranscriptEntry]:
    recalled = aliased(Conversation)
    rows = await session.execute(
        select(
            Entry.kind,
            Entry.source,
            Entry.text,
            Entry.recalled_conversation_id,
            recalled.title,
            Entry.reminder_id,
            Entry.id,
        )
        .outerjoin(recalled, recalled.id == Entry.recalled_conversation_id)
        .where(Entry.conversation_id == conversation_id)
        .order_by(Entry.id)
    )
    entries = [TranscriptEntry(*row) for row in rows]
    refs = await refs_by_entry(session, (e.entry_id for e in entries if e.kind == "user"))
    return [
        dataclasses.replace(e, attachments=refs[e.entry_id]) if e.entry_id in refs else e
        for e in entries
    ]


def _line(e: TranscriptEntry) -> str:
    match e.kind:
        case "user":
            return f"User: {with_handles(e.text, (handle(a) for a in e.attachments))}"
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


def _omitted(n: int) -> str:
    return f"\n[… {n} characters omitted …]\n"


def _local_date(moment: datetime, zone: ZoneInfo) -> str:
    """``Tue 3 Nov 2026``, in alfred's timezone."""
    day = moment.astimezone(zone)
    return f"{day:%a} {day.day} {day:%b %Y}"
