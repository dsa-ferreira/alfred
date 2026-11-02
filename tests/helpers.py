"""Test helpers: a config, a scripted fake model, queue rows."""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.model import ModelReply, ToolCall
from alfred.config import (
    AIConfig,
    Config,
    HistoryConfig,
    MCPConfig,
    McpServer,
    MemoryConfig,
    QueueConfig,
    ServerConfig,
    Service,
    TelegramConfig,
)
from alfred.db.models import QueueItem

OWNER = 42


def make_config(
    *,
    ai: dict[str, Any] | None = None,
    history: dict[str, Any] | None = None,
    queue: dict[str, Any] | None = None,
    memory: dict[str, Any] | None = None,
    mcp_servers: Sequence[McpServer] = (),
    services: Sequence[Service] = (),
) -> Config:
    return Config(
        telegram=TelegramConfig(owner_user_id=OWNER),
        ai=AIConfig(**(ai or {})),
        history=HistoryConfig(**(history or {})),
        queue=QueueConfig(**(queue or {})),
        mcp=MCPConfig(list_timeout_s=5, call_timeout_s=5),
        server=ServerConfig(),
        services=tuple(services),
        mcp_servers=tuple(mcp_servers),
        telegram_bot_token=SecretStr("123:test"),
        database_url=SecretStr("postgresql+psycopg://unused"),
        memory=MemoryConfig(**(memory or {})),
    )


def text(content: str) -> ModelReply:
    return ModelReply(content=content)


def calls(*specs: tuple[str, str]) -> ModelReply:
    return ModelReply(
        content=None,
        tool_calls=[ToolCall(f"c{i}", name, args) for i, (name, args) in enumerate(specs)],
    )


class FakeModel:
    """Plays back scripted replies (or raises scripted exceptions) and records requests."""

    def __init__(self, script: Sequence[ModelReply | Exception] = ()) -> None:
        self.script = list(script)
        self.requests: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
        self.models: list[str | None] = []  # the model each request asked for

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        model: str | None = None,
    ) -> ModelReply:
        self.requests.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        self.models.append(model)
        if not self.script:
            raise AssertionError("the fake model ran out of scripted replies")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def tool_names(self, request: int = 0) -> set[str]:
        return {t["function"]["name"] for t in self.requests[request][1]}


async def add_queue_item(
    sessions: async_sessionmaker[AsyncSession],
    kind: str = "owner",
    text_: str = "hi",
    source: str | None = None,
) -> int:
    async with sessions.begin() as session:
        item = QueueItem(kind=kind, text=text_, source=source)
        session.add(item)
        await session.flush()
        return item.id


class FakeEmbedder:
    """Returns scripted vectors (or raises) and records what it embedded.

    Without a script, every text gets ``default``.
    """

    def __init__(
        self,
        script: Sequence[list[float] | Exception] = (),
        *,
        model: str = "nomic-embed-text:v1.5",
        default: list[float] | None = None,
    ) -> None:
        self.model = model
        self.script = list(script)
        self.default = default
        self.documents: list[str] = []
        self.queries: list[str] = []

    async def embed_document(self, text: str) -> list[float]:
        self.documents.append(text)
        return self._next()

    async def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return self._next()

    def _next(self) -> list[float]:
        if self.script:
            step = self.script.pop(0)
        elif self.default is not None:
            step = self.default
        else:
            raise AssertionError("the fake embedder ran out of scripted vectors")
        if isinstance(step, Exception):
            raise step
        return step
