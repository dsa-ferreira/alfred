"""Question messages: their title, Telegram HTML and buttons, and their outcome.

Written by alfred, never by the model. The title says what asked: the user's
own request, the service of an ``/ask``, or the reminder by number and text.
``detail`` (a call's arguments as JSON) is preformatted and cut, with a
marker, so the whole message fits one Telegram message with its buttons and
the outcome line added later.

Each button's ``callback_data`` is ``ir:<request id>:<option index>``, well
under Telegram's 64 bytes.
"""

from __future__ import annotations

import re
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from alfred.tools.base import CANCEL, DO_IT, Question

_TELEGRAM_MAX_CHARS = 4096
# Room kept for the outcome line an edit adds.
_OUTCOME_RESERVE = 200
_MAX_BODY_CHARS = 1000
_BUTTONS_PER_ROW = 2
_CALLBACK = re.compile(r"ir:(\d+):(\d+)")


ENDED_LINES = {
    "new_message": "Cancelled: you sent a new message.",
    "expired": "Expired: no answer in time.",
    "not_delivered": "Cancelled.",
}


def title(kind: str, *, source: str | None = None, reminder: str | None = None) -> str:
    """What asked, by the queue item's kind: ``user``, ``ask`` or ``reminder``.

    ``reminder`` is ``#12 (call mum)``.
    """
    if kind == "ask":
        return f"For a request from {source}:"
    if kind == "reminder":
        return f"For reminder {reminder}:"
    return "For your request:"


def reminder_label(reminder_id: int | None, text: str) -> str:
    short = text if len(text) <= 60 else text[:59] + "…"
    return f"#{reminder_id} ({short})"


def question_html(question: Question) -> str:
    """The message text: the title in bold, the question, and the detail in ``<pre>``."""
    body = _cut(question.body, _MAX_BODY_CHARS)
    lines = [f"<b>{escape(question.title)}</b>", escape(body)] if question.title else [escape(body)]
    if question.detail:
        used = len(question.title) + len(body) + 2
        budget = _TELEGRAM_MAX_CHARS - _OUTCOME_RESERVE - used
        lines.append(f"<pre>{escape(_cut(question.detail, budget))}</pre>")
    return "\n".join(lines)


def keyboard(request_id: int, question: Question) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=o.label, callback_data=f"ir:{request_id}:{i}")
        for i, o in enumerate(question.options)
    ]
    rows = [buttons[i : i + _BUTTONS_PER_ROW] for i in range(0, len(buttons), _BUTTONS_PER_ROW)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def parse_callback(data: str | None) -> tuple[int, int] | None:
    """``(request id, option index)`` from a button's data, or ``None``."""
    found = _CALLBACK.fullmatch(data or "")
    return (int(found.group(1)), int(found.group(2))) if found else None


def answered_line(label: str) -> str:
    if label == DO_IT:
        return "Confirmed."
    if label == CANCEL:
        return "Cancelled."
    return f"Answered: {label}"


def outcome_html(question: Question, line: str) -> str:
    """The question message once it is over: as it was, then what happened (no buttons)."""
    return f"{question_html(question)}\n\n<i>{escape(line)}</i>"


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n… [cut: {} more characters]"
    keep = max(limit - len(marker.format(len(text))), 0)
    return text[:keep] + marker.format(len(text) - keep)
