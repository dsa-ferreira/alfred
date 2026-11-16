"""Test helpers: a config, a scripted fake model, an agent, queue rows."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Callable, Collection, Sequence
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.adapters.http.config import ServerConfig, Service
from alfred.app import _build_toolsets
from alfred.config import Config, _AIConfig, _MemoryConfig
from alfred.features.archive.summaries import Summaries
from alfred.features.archive.summary_job import SummaryJob
from alfred.features.archive.tools import Searcher
from alfred.features.attachments import NewAttachment
from alfred.features.attachments.config import AttachmentsConfig, S3Key
from alfred.features.attachments.store import StoreError, _StoredObject
from alfred.features.attachments.tools import AttachmentTools
from alfred.features.brains.registry import Brains
from alfred.features.conversations.config import HistoryConfig
from alfred.features.conversations.store import History
from alfred.features.facts.tools import FactTools
from alfred.features.mcp.client import Connector
from alfred.features.mcp.config import McpConfig, McpServer
from alfred.features.reminders.config import RemindersConfig
from alfred.features.reminders.store import Reminders
from alfred.features.reminders.tools import ReminderTools
from alfred.features.web.config import WebConfig
from alfred.features.web.tools import WebTools
from alfred.infra.llm.chat import ModelReply, ToolCall
from alfred.kernel.access import AllowList
from alfred.kernel.turn import TurnInput
from alfred.kernel.users import User, Users
from alfred.runtime.agent.framing import Framing
from alfred.runtime.agent.inline import InlineAttachments
from alfred.runtime.agent.results import TurnResult
from alfred.runtime.agent.turn import Agent, _Model
from alfred.runtime.turns.background import Sweeper
from alfred.runtime.turns.call_log import ToolCallLog
from alfred.runtime.turns.config import QueueConfig
from alfred.runtime.turns.due_reminders import _claim_once
from alfred.runtime.turns.exchange import Exchange
from alfred.runtime.turns.inputs.endings import Endings
from alfred.runtime.turns.intake import ChatIntake
from alfred.runtime.turns.kinds import Kinds
from alfred.runtime.turns.kinds.ask import AskPayload
from alfred.runtime.turns.kinds.chat import ChatPayload
from alfred.runtime.turns.kinds.reminder import ReminderPayload
from alfred.runtime.turns.queue import Queue, read_item
from alfred.runtime.turns.tables import QueueItem
from tests.fakes import NoFacts

OWNER = 42  # diogo's Telegram user ID
ANA_ID = 222
# The admin, offered every tool.
DIOGO = User("diogo", OWNER, "sir", admin=True, tools=AllowList.of("*"))
# A second user with a partial allow-list (the users spec's example).
ANA = User(
    "ana",
    ANA_ID,
    "madam",
    tools=AllowList.of("alfred_*", "cookbook_*", "mcparr_find_media", "mcparr_add_media"),
)

# Every kind, with reminder notices in UTC (the worker's default).
KINDS = Kinds.default(ZoneInfo("UTC"))


def framing(turn: TurnInput) -> Framing:
    """How the worker frames ``turn`` for the agent: its kind's framing."""
    return KINDS.of(turn).framing(turn)


class FramingAgent(Agent):
    """An agent whose ``run`` frames the turn by its kind when not told, as the worker does."""

    async def run(
        self,
        turn: TurnInput,
        framing_: Framing | None = None,
        *,
        hidden_items: Collection[int] = (),
    ) -> TurnResult:
        return await super().run(turn, framing_ or framing(turn), hidden_items=hidden_items)


def make_endings(
    sessions: async_sessionmaker[AsyncSession],
    users: Users,
    history: History | None = None,
    kinds: Kinds = KINDS,
) -> Endings:
    """Records ended input requests' turns, as ``serve`` wires it (nothing is sent)."""
    return Endings(sessions, Exchange(history or History(sessions), _NoOutbox()), kinds, users)


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
    users: Sequence[User] = (DIOGO,),
) -> Config:
    return Config(
        users=Users(tuple(users)),
        ai=_AIConfig(**(ai or {})),
        history=HistoryConfig(**(history or {})),
        queue=QueueConfig(**(queue or {})),
        mcp=McpConfig(list_timeout_s=5, call_timeout_s=5),
        server=ServerConfig(),
        services=tuple(services),
        mcp_servers=tuple(mcp_servers),
        telegram_bot_token=SecretStr("123:test"),
        database_url=SecretStr("postgresql+psycopg://unused"),
        memory=_MemoryConfig(**(memory or {})),
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

    async def complete_streamed(
        self, messages: list[dict[str, Any]], *, model: str | None = None
    ) -> ModelReply:
        """Summaries stream their request; the fake answers it like any other."""
        return await self.complete(messages, [], model=model)

    def tool_names(self, request: int = 0) -> set[str]:
        return {t["function"]["name"] for t in self.requests[request][1]}


async def add_queue_item(
    sessions: async_sessionmaker[AsyncSession],
    kind: str = "chat",
    text_: str = "hi",
    source: str | None = None,
    user: str = "diogo",
    *,
    reminder_id: int = 1,
    due_at: datetime | None = None,
    created_by: str | None = None,
) -> int:
    """A pending queue item of ``kind`` for ``user``; its text is the message, prompt or
    reminder. Reminder items are a snapshot of reminder ``reminder_id``, due now."""
    payload: ChatPayload | AskPayload | ReminderPayload
    match kind:
        case "chat":
            payload = ChatPayload(text_)
        case "ask":
            payload = AskPayload(source or "backup", text_)
        case "reminder":
            payload = ReminderPayload(
                reminder_id, due_at or datetime.now(UTC), text_, created_by or user
            )
        case _:
            raise ValueError(f"no queue kind {kind!r}")
    async with sessions.begin() as session:
        return await Queue(sessions).add(session, user, payload)


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
    model: _Model,
    history: History,
    sessions: async_sessionmaker[AsyncSession],
    connect: Connector,
    *,
    memory: Searcher | None = None,
    facts: FactTools | None = None,
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
        attachments = AttachmentTools(store, sessions, brains, config.attachments, history)
        inline = InlineAttachments(store, config.attachments)
    toolsets = _build_toolsets(
        config,
        connect,
        brains=brains,
        web=web,
        memory=memory,
        facts=facts,
        reminders=reminders,
        attachments=attachments,
        now=now,
    )
    return FramingAgent(
        model,
        history,
        ToolCallLog(sessions),
        **agent_settings(config),
        toolsets=toolsets,
        brains=brains,
        attachments=inline,
        facts=facts.facts if facts is not None else NoFacts(),
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

    async def list(self, prefix: str) -> AsyncIterator[_StoredObject]:
        self._check("list", prefix)
        for key, (_, _, modified) in sorted(self.objects.items()):
            if key.startswith(prefix):
                yield _StoredObject(key, modified)

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
    user: str = "diogo",
) -> tuple[int, int]:
    """Queue ``user``'s message with files and record its entry, as a turn would.

    With ``store``, each file's bytes (``data``, in order) are put there.
    Returns the queue item's and the entry's IDs.
    """
    for file, body in zip(files, data, strict=False):
        assert store is not None
        await store.put(file.object_key, body, file.mime)
    item = await ChatIntake(sessions, Queue(sessions)).enqueue_user(user, text_, files)
    async with sessions() as session:
        queued = await read_item(session, item)
    assert queued is not None and isinstance(queued.payload, ChatPayload)
    entry = await History(sessions).append(
        user, "user", text_, turn_id=item, attachment_ids=queued.payload.attachment_ids
    )
    return item, entry


def make_sweeper(
    cfg: Config,
    sessions: async_sessionmaker[AsyncSession],
    summaries: Summaries,
    *,
    queue: Any,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    jobs: Sequence[Any] = (),
) -> Sweeper:
    """The background job runner as ``serve`` builds it: the summary job, then ``jobs``."""
    return Sweeper(
        [SummaryJob(sessions, summaries, now), *jobs],
        queue=queue,
        quiet_s=cfg.memory.summary_quiet_s,
        retry=cfg.queue,
    )


ADDRESSES = ("sir", "Mr. Ferreira", "madam")


def assert_addressed(text: str, address: str, *, after: str = "") -> None:
    """A fixed notice ends its first clause, before ``after``, with ``address``, once."""
    assert text.removesuffix(after).endswith(f", {address}.")
    assert text.count(address) == 1


def agent_settings(cfg: Config) -> dict[str, Any]:
    """The config parts ``serve`` gives the agent."""
    return {"ai": cfg.ai, "window": cfg.history, "memory": cfg.memory, "users": cfg.users}


async def claim_due(reminders: Reminders, now: datetime) -> list[int]:
    """Queue the reminders due by ``now``, as the due loop does; the queue item IDs."""
    sessions = reminders._sessions
    return await _claim_once(sessions, reminders, Queue(sessions), now)


def reminder_of(item: QueueItem) -> ReminderPayload:
    """A reminder queue item's payload."""
    assert item.kind == "reminder", item.kind
    return ReminderPayload.from_json(item.payload)


class _NoOutbox:
    async def send(self, user: User, text_: str) -> None:
        raise AssertionError("an ending is recorded, never sent")
