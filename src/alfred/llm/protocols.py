"""What background jobs need from the model servers, so tests can fake them."""

from __future__ import annotations

from typing import Any, Protocol

from alfred.llm.chat import ModelReply


class ChatModel(Protocol):
    async def complete_streamed(self, messages: list[dict[str, Any]]) -> ModelReply: ...


class Embedder(Protocol):
    model: str

    async def embed_document(self, text: str) -> list[float]: ...

    async def embed_query(self, text: str) -> list[float]: ...
