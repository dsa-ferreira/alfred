"""Test helpers: a config, a scripted fake model, an agent, queue rows."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.brains import Brains
from alfred.agent.model import ModelReply, ToolCall
from alfred.agent.turn import Agent, Model
from alfred.app import build_toolsets
from alfred.attachments import NewAttachment
from alfred.attachments.inline import InlineAttachments
from alfred.attachments.store import StoredObject, StoreError
from alfred.attachments.tools import AttachmentTools
from alfred.config import (
    AIConfig,
    AttachmentsConfig,
    Config,
    HistoryConfig,
    MCPConfig,
    McpServer,
    MemoryConfig,
    QueueConfig,
    RemindersConfig,
    S3Key,
    ServerConfig,
    Service,
    TelegramConfig,
    WebConfig,
)
from alfred.db.models import QueueItem
from alfred.history import History
from alfred.mcp.client import Connector
from alfred.memory.search import Searcher
from alfred.queue import Queue
from alfred.reminders.tools import ReminderTools
from alfred.web.tools import WebTools

OWNER = 42


def make_config(
    *,
    ai: dict[str, Any] | None = None,
    history: dict[str, Any] | None = None,
    queue: dict[str, Any] | None = None,
    memory: dict[str, Any] | None = None,
    web: dict[str, Any] | None = None,
    reminders: dict[str, Any] | None = None,
    attachments: dict[str, Any] | None = None,
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
        web=WebConfig(**(web or {})),
        reminders=RemindersConfig(**(reminders or {})),
        attachments=AttachmentsConfig(**attachments) if attachments is not None else None,
        s3_key=S3Key(SecretStr("key-id"), SecretStr("secret")) if attachments is not None else None,
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


def make_agent(
    config: Config,
    model: Model,
    history: History,
    sessions: async_sessionmaker[AsyncSession],
    connect: Connector,
    *,
    memory: Searcher | None = None,
    web: WebTools | None = None,
    reminders: ReminderTools | None = None,
    brains: Brains | None = None,
    store: FakeStore | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Agent:
    """An agent with the toolsets ``serve`` gives it, for the parts passed in.

    With ``store`` and ``config.attachments``, attachments are on, as ``serve`` does.
    """
    brains = brains or Brains.from_config(config.ai)
    attachments = inline = None
    if store is not None and config.attachments is not None:
        attachments = AttachmentTools(store, sessions, brains, config.attachments)
        inline = InlineAttachments(store, config.attachments)
    toolsets = build_toolsets(
        config,
        connect,
        brains=brains,
        web=web,
        memory=memory,
        reminders=reminders,
        attachments=attachments,
        now=now,
    )
    return Agent(
        config,
        model,
        history,
        sessions,
        toolsets=toolsets,
        brains=brains,
        attachments=inline,
        now=now,
    )


class FakeStore:
    """An in-memory ``ObjectStore``. Set ``failing`` to make every request fail."""

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.objects: dict[str, tuple[bytes, str, datetime]] = {}
        self.failing = False
        self.gets: list[str] = []  # the keys read, in order
        self._now = now

    def _check(self, operation: str, key: str) -> None:
        if self.failing:
            raise StoreError(f"object store {operation} {key} failed: ConnectionRefused")

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self._check("put", key)
        self.objects[key] = (data, content_type, self._now())

    async def get(self, key: str) -> bytes:
        self._check("get", key)
        self.gets.append(key)
        if key not in self.objects:
            raise StoreError(f"object store get {key} failed: NoSuchKey")
        return self.objects[key][0]

    async def list(self, prefix: str) -> AsyncIterator[StoredObject]:
        self._check("list", prefix)
        for key, (_, _, modified) in sorted(self.objects.items()):
            if key.startswith(prefix):
                yield StoredObject(key, modified)

    async def delete(self, key: str) -> None:
        self._check("delete", key)
        self.objects.pop(key, None)


def image_file(key: str = "attachments/photo", name: str | None = None) -> NewAttachment:
    return NewAttachment("image", "image/jpeg", 1000, key, name, width=800, height=600)


def pdf_file(
    pages: list[str], key: str = "attachments/doc", name: str | None = "invoice.pdf"
) -> NewAttachment:
    return NewAttachment("pdf", "application/pdf", 2000, key, name, pages=pages)


async def owner_with_files(
    sessions: async_sessionmaker[AsyncSession],
    text_: str,
    files: Sequence[NewAttachment],
    store: FakeStore | None = None,
    data: Sequence[bytes] = (),
) -> tuple[int, int]:
    """Queue an owner message with files and record its entry, as a turn would.

    With ``store``, each file's bytes (``data``, in order) are put there.
    Returns the queue item's and the entry's IDs.
    """
    for file, body in zip(files, data, strict=False):
        assert store is not None
        await store.put(file.object_key, body, file.mime)
    item = await Queue(sessions).enqueue_owner(text_, None, files)
    entry = await History(sessions).append("owner", text_, queue_item_id=item)
    return item, entry
