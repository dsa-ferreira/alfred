"""Images and PDFs from the owner (change add-attachments)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

AttachmentKind = Literal["image", "pdf"]


class Unreadable(ValueError):
    """A file alfred can't use: not a supported image, or a PDF it can't open."""


@dataclass(frozen=True)
class NewAttachment:
    """A file already in the object store, to be recorded with its queue item."""

    kind: AttachmentKind
    mime: str
    size_bytes: int
    object_key: str
    file_name: str | None = None
    width: int | None = None
    height: int | None = None
    # A PDF's text, page 1 first.
    pages: list[str] = field(default_factory=list)

    @property
    def page_count(self) -> int | None:
        return len(self.pages) if self.kind == "pdf" else None
