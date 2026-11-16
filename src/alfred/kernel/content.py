"""Content for the model beyond text."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelImage:
    """An image ready for the model: already scaled down and encoded."""

    data: bytes
    mime: str  # image/jpeg or image/png

    def part(self) -> dict[str, Any]:
        """The chat-completions content part that carries it."""
        return {"type": "image_url", "image_url": {"url": self._data_url()}}

    def _data_url(self) -> str:
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode('ascii')}"
