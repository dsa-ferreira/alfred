"""Questions a waiting tool call asks its user, and the buttons that answer them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DO_IT = "Do it"
CANCEL = "Cancel"


@dataclass(frozen=True)
class Option:
    """A button: its label, and the JSON-able answer the toolset gets back when it's chosen."""

    label: str
    answer: Any


@dataclass(frozen=True)
class Question:
    """What a waiting call asks its user.

    ``title`` says what asked for it (the user's request, a service, a
    reminder) and is filled in by the worker; ``body`` is the question,
    ``detail`` preformatted text shown under it (the arguments as JSON).
    """

    body: str
    options: tuple[Option, ...]
    detail: str | None = None
    title: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "body": self.body,
            "detail": self.detail,
            "options": [{"label": o.label, "answer": o.answer} for o in self.options],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Question:
        return cls(
            body=data["body"],
            options=tuple(Option(o["label"], o["answer"]) for o in data["options"]),
            detail=data.get("detail"),
            title=data.get("title", ""),
        )


@dataclass(frozen=True)
class InputNeed:
    """A call that waits for its user's answer: the question, and what resuming it needs.

    ``state`` is JSON-able and handed back to the toolset's ``resume``.
    """

    question: Question
    state: dict[str, Any]
