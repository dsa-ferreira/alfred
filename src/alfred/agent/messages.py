"""The model's messages: a conversation's history entries as chat-completions messages.

Every ``user`` turn starts with the time its entry was recorded (a stamp such
as ``[Mon 9 Nov 14:05]``), absolute so an entry's text never changes and the
AI server can keep reusing its processed prefix.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from alfred.attachments.handles import handle, with_handles
from alfred.conversations.store import HistoryEntry
from alfred.conversations.transcript import recall_label
from alfred.kernel.content import ModelImage
from alfred.times import format_when

_UTC_ZONE = ZoneInfo("UTC")
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_STAMP = re.compile(
    r"\[(?:" + "|".join(_DAYS) + r") \d{1,2} (?:" + "|".join(_MONTHS) + r") \d{2}:\d{2}\]"
)
_LEADING_STAMP = re.compile(_STAMP.pattern + r"\s*")


@dataclass(frozen=True)
class Inline:
    """How a user entry in the inline window is sent.

    ``handles`` replace the plain handles (a PDF's with its page 1 text, a
    note when an image can't be shown); ``images`` follow the text as image
    parts.
    """

    handles: list[str]
    images: list[ModelImage] = field(default_factory=list)


def stamped(text_: str, moment: datetime | None, zone: ZoneInfo) -> str:
    """``text_`` after the time it arrived, or as is when that time is unknown."""
    return text_ if moment is None else f"{_format_stamp(moment, zone)} {text_}"


def strip_stamp(text_: str) -> str:
    """``text_`` without a stamp at its very start (one the model copied)."""
    return _LEADING_STAMP.sub("", text_, count=1) if _LEADING_STAMP.match(text_) else text_


def to_messages(
    entries: list[HistoryEntry],
    zone: ZoneInfo = _UTC_ZONE,
    inline: Mapping[int, Inline] | None = None,
) -> list[dict[str, Any]]:
    """Map entries to chat-completions messages.

    User messages and ask prompts are ``user`` turns (ask prompts prefixed
    ``[ask:<service>]``), replies are ``assistant`` turns, and notices are
    ``user`` turns prefixed ``[notice:<service>]``. Delivered reminders are
    ``user`` turns prefixed ``[reminder:#<id> due <time in zone>]`` (with
    ``, set by <name>`` when someone else set it), followed by what the
    user was told. Recalls are ``user`` turns prefixed ``[recall:#<id>
    "<title>"]``: the newest with its transcript, older ones with only the
    recalled conversation's summary.

    Every ``user`` turn starts with the time its entry was recorded, before
    its label (``[Mon 9 Nov 14:05] [notice:backup] ...``); replies have none.

    A user message ends with the handles of its attachments
    (``[attachment #7: image "receipt.jpg"]``). The user entries in
    ``inline`` (by entry ID) are sent as it says; with images, their content
    is a list: the text part, then one ``image_url`` part per image.
    """
    newest_recall = max((i for i, e in enumerate(entries) if e.kind == "recall"), default=None)
    messages: list[dict[str, Any]] = []
    for i, e in enumerate(entries):
        content: str | None = None
        match e.kind:
            case "recall":
                label = f"[recall:{recall_label(e.recalled_id, e.recalled_title)}]"
                if i == newest_recall:
                    content = f"{label} {e.text}"
                else:
                    content = f"{label} Summary: {e.recalled_summary or '(no summary yet)'}"
            case "user":
                extra = (inline or {}).get(e.entry_id) if e.entry_id is not None else None
                content = _user_text(e, extra)
                if extra is not None and extra.images:
                    text_part = {"type": "text", "text": stamped(content, e.created_at, zone)}
                    parts = [text_part, *(image.part() for image in extra.images)]
                    messages.append({"role": "user", "content": parts})
                    continue
            case "ask_prompt":
                content = f"[ask:{e.source}] {e.text}"
            case "notice":
                content = f"[notice:{e.source}] {e.text}"
            case "reminder":
                due = f" due {format_when(e.reminder_due_at, zone)}" if e.reminder_due_at else ""
                set_by = f", set by {e.reminder_set_by}" if e.reminder_set_by else ""
                content = f"[reminder:#{e.reminder_id}{due}{set_by}] {e.text}"
            case "assistant" | "ask_reply" | "reminder_reply":
                messages.append({"role": "assistant", "content": e.text})
        if content is not None:
            messages.append({"role": "user", "content": stamped(content, e.created_at, zone)})
    return messages


def _format_stamp(moment: datetime, zone: ZoneInfo) -> str:
    """``[Mon 9 Nov 14:05]``: absolute, so an entry's text never changes.

    English names without the locale, like the rest of alfred's prompts.
    """
    local = moment.astimezone(zone)
    return f"[{_DAYS[local.weekday()]} {local.day} {_MONTHS[local.month - 1]} {local:%H:%M}]"


def _user_text(entry: HistoryEntry, inline: Inline | None = None) -> str:
    """What the user wrote, then the handles of what they sent with it."""
    handles = inline.handles if inline else [handle(a) for a in entry.attachments]
    return with_handles(entry.text, handles)
