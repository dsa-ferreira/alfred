"""``alfred serve``: the bot, the service API, the AI worker, the memory sweeper,
the due-reminder loop, the input request expiry loop, with ``[attachments]``
the orphan file sweeper, and with ``[history] idle_close_after_h`` the idle
conversation closer.

Six asyncio tasks (more with those) share the database engine and the queue's
in-process signal. SIGTERM or SIGINT stops them all; a turn in progress is
interrupted and its item stays ``pending``, so it runs again after restart, a
summary in progress is simply made again later, and a reminder claim is one
transaction, so it either happened or happens again. If any task stops on its
own (for example the port is taken), the others are stopped too and ``serve``
raises.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import uvicorn
from aiogram import Bot, Dispatcher
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.inline import InlineAttachments
from alfred.agent.turn import Agent
from alfred.archive.search import ConversationSearch
from alfred.archive.summaries import Summaries, recalled
from alfred.archive.summary_job import SummaryJob
from alfred.archive.tools import MemoryTools, Searcher
from alfred.attachments.intake import Intake
from alfred.attachments.store import S3Store
from alfred.attachments.sweeper import OrphanSweeper
from alfred.attachments.tools import AttachmentTools
from alfred.brains.registry import Brains
from alfred.brains.tools import BrainTools
from alfred.config import Config
from alfred.conversations.idle import IdleCloser
from alfred.conversations.store import History
from alfred.conversations.tools import ConversationTools
from alfred.db.engine import make_engine, make_session_factory
from alfred.facts.duplicate_job import FactDuplicateJob
from alfred.facts.embedding_job import FactEmbeddingJob
from alfred.facts.extraction_job import FactExtractionJob
from alfred.facts.store import FactStore
from alfred.facts.tools import FactTools
from alfred.http.api import create_app
from alfred.kernel.toolset import Toolset
from alfred.llm.chat import ModelClient
from alfred.llm.embedding import EmbeddingClient
from alfred.mcp.client import Connector, http_connector
from alfred.mcp.toolset import McpTools
from alfred.reminders.tools import ReminderTools
from alfred.startup import check_tool_names, warn_unknown_users
from alfred.telegram.bot import (
    ALLOWED_UPDATES,
    MemoryCommand,
    TelegramDownloader,
    TelegramOutbox,
    make_dispatcher,
    register_commands,
)
from alfred.telegram.taps import Taps
from alfred.toolbox.calculator import CalculatorTools
from alfred.toolbox.dates import DateTools
from alfred.turns.background import Sweeper
from alfred.turns.call_log import ToolCallLog
from alfred.turns.due_reminders import run_due_loop
from alfred.turns.exchange import Exchange
from alfred.turns.inputs.endings import Endings
from alfred.turns.inputs.loop import cancel_on_new_message, run_expiry_loop
from alfred.turns.inputs.parking import Parking
from alfred.turns.inputs.store import InputRequests
from alfred.turns.intake import ChatIntake
from alfred.turns.kinds import Kinds
from alfred.turns.queue import Queue, busy
from alfred.turns.worker import Worker
from alfred.web.tools import WebTools

log = logging.getLogger(__name__)

# alfred's own toolsets, in the order ``_build_toolsets`` offers them.
_TOOLSET_TYPES = (
    WebTools,
    ConversationTools,
    MemoryTools,
    FactTools,
    AttachmentTools,
    ReminderTools,
    BrainTools,
    CalculatorTools,
    DateTools,
)

# The tasks stopped by cancelling them; the API and polling stop on their own.
_CANCELLED_ON_STOP = frozenset(
    {"worker", "memory", "reminders", "input-requests", "attachments", "idle"}
)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True
    )
    # Per-request lines from the HTTP and S3 clients are noise at INFO, and
    # botocore's DEBUG lines show signed request headers.
    for name in ("httpx", "httpx2", "aiogram.event", "botocore", "aiobotocore"):
        logging.getLogger(name).setLevel(max(logging.WARNING, logging.getLogger().level))


async def serve(config: Config) -> None:
    """Run everything until stopped.

    Raises :class:`~alfred.settings.ConfigError` before connecting to anything
    when an allow-list names a tool alfred doesn't have.
    """
    check_tool_names(config.users, _TOOLSET_TYPES)
    async with contextlib.AsyncExitStack() as stack:
        services = await _build_services(config, stack)
        _log_startup(config, services.brains)
        failed = await _run_tasks(config, services)
    if failed is not None:
        exc = failed.exception() if not failed.cancelled() else None
        cause = f": {type(exc).__name__}: {exc}" if exc is not None else ""
        # Belt and braces: error texts never carry the bot token, but make sure.
        cause = cause.replace(config.telegram_bot_token.get_secret_value(), "<bot token>")
        raise RuntimeError(f"{failed.get_name()} stopped unexpectedly{cause}") from exc
    log.info("alfred stopped")


class _Server(uvicorn.Server):
    """uvicorn without its own signal handlers; ``serve`` owns shutdown."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


def _build_toolsets(
    config: Config,
    connect: Connector,
    *,
    brains: Brains,
    web: WebTools | None = None,
    memory: Searcher | None = None,
    facts: FactTools | None = None,
    reminders: ReminderTools | None = None,
    attachments: AttachmentTools | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[Toolset]:
    """What the model may call, in the order it is offered; alfred's own tools first.

    Each toolset decides on which turns it is offered; the turn user's
    allow-list then gates every one of them (``alfred.agent.turn``). A name
    offered by two toolsets goes to the first.
    """
    toolsets: list[Toolset] = []
    if web is not None:
        toolsets.append(web)
    toolsets.append(ConversationTools())
    if memory is not None:
        toolsets.append(MemoryTools(memory))
    if facts is not None:
        toolsets.append(facts)
    # Only with [attachments].
    if attachments is not None:
        toolsets.append(attachments)
    if reminders is not None:
        toolsets.append(reminders)
    toolsets.append(BrainTools(brains))
    # Arithmetic and dates, on every turn.
    toolsets.append(CalculatorTools())
    toolsets.append(DateTools(ZoneInfo(config.ai.timezone), now=now))
    toolsets.append(
        McpTools.from_config(
            config.mcp, config.mcp_servers, connect, max_chars=config.ai.tool_result_max_chars
        )
    )
    return toolsets


@dataclass(frozen=True)
class _Services:
    """What the tasks run, once built."""

    server: _Server
    sessions: async_sessionmaker[AsyncSession]
    bot: Bot
    dispatcher: Dispatcher
    worker: Worker
    sweeper: Sweeper
    queue: Queue
    reminders: ReminderTools
    inputs: InputRequests
    endings: Endings
    outbox: TelegramOutbox
    brains: Brains
    orphans: OrphanSweeper | None
    idle: IdleCloser | None


async def _build_services(config: Config, stack: contextlib.AsyncExitStack) -> _Services:
    """Build everything ``serve`` runs; each resource's close goes on ``stack`` as it is made.

    The stack closes them in reverse order, the database engine last.
    """
    engine = make_engine(config.database_url.get_secret_value())
    stack.push_async_callback(engine.dispose)
    sessions = make_session_factory(engine)
    # A renamed user shows up here.
    await warn_unknown_users(sessions, config.users.names)
    queue = Queue(sessions)
    bot = Bot(config.telegram_bot_token.get_secret_value())
    stack.push_async_callback(bot.session.close)
    outbox = TelegramOutbox(bot)
    model = ModelClient(config.ai.base_url, config.ai.model, config.ai.request_timeout_s)
    stack.push_async_callback(model.aclose)
    # A second client only when summaries use another model.
    summary_tag = config.memory.summary_model_or(config.ai.model)
    summary_model = (
        model
        if summary_tag == config.ai.model
        else ModelClient(config.ai.base_url, summary_tag, config.ai.request_timeout_s)
    )
    if summary_model is not model:
        stack.push_async_callback(summary_model.aclose)
    embedder = EmbeddingClient(
        config.memory.embedding_base_url,
        config.memory.embedding_model,
        config.memory.embedding_timeout_s,
        document_prefix=config.memory.document_prefix,
        query_prefix=config.memory.query_prefix,
    )
    stack.push_async_callback(embedder.aclose)
    summaries = Summaries(config.memory, config.queue, sessions, summary_model, embedder)
    # A conversation with no user message is archived when something new arrives
    # after the idle close time; the sweeper then summarises it.
    idle_after = (
        timedelta(hours=config.history.idle_close_after_h)
        if config.history.idle_close_after_h is not None
        else None
    )
    history = History(
        sessions,
        idle_after=idle_after,
        on_archive=summaries.changed.set,
        recalled=recalled,
    )
    # Chat, /ask and reminder turns: what each records and sends.
    kinds = Kinds.default(ZoneInfo(config.ai.timezone))
    # Turns waiting for their user's answer: a user's new message
    # cancels theirs before it is queued, and the turns that end
    # unanswered are recorded through their kinds.
    inputs = InputRequests(sessions)
    endings = Endings(sessions, Exchange(history, outbox), kinds, config.users)
    chat = ChatIntake(
        sessions,
        queue,
        before_message=cancel_on_new_message(inputs, endings, outbox, config.users),
    )
    # User facts: extraction after the summaries, then embedding, which
    # needs no quiet queue, then the duplicate check.
    facts = FactStore(config.memory, config.queue, sessions, embedder)
    sweeper = Sweeper(
        [
            # Summaries feed search, which users see: first.
            SummaryJob(sessions, summaries),
            FactExtractionJob(
                config.memory,
                config.queue,
                sessions,
                summary_model,
                facts,
                input_max_chars=config.memory.summary_input_max_chars,
                recalled=recalled,
            ),
            FactEmbeddingJob(facts),
            # Needs the model, so only while the queue is quiet.
            FactDuplicateJob(facts, summary_model),
        ],
        queue=queue,
        quiet_s=config.memory.summary_quiet_s,
        retry=config.queue,
    )
    search = ConversationSearch(config.memory, ZoneInfo(config.ai.timezone), sessions, embedder)
    # Turns pass the current brain's tag per request; summaries never do, so
    # they stay on the summary model.
    brains = Brains.from_config(config.ai)
    # Web search and fetch share one HTTP client.
    web = WebTools.from_config(config.web, max_chars=config.ai.tool_result_max_chars)
    stack.push_async_callback(web.aclose)
    reminders = ReminderTools.from_config(
        config.reminders, ZoneInfo(config.ai.timezone), config.users, sessions
    )
    # Photos, image files and PDFs, only with [attachments].
    store: S3Store | None = None
    intake: Intake | None = None
    attachment_tools: AttachmentTools | None = None
    inline: InlineAttachments | None = None
    orphans: OrphanSweeper | None = None
    if config.attachments is not None:
        assert config.s3_key is not None
        store = await S3Store.open(config.attachments, config.s3_key)
        stack.push_async_callback(store.aclose)
        intake = Intake(config.attachments, store, chat, TelegramDownloader(bot))
        attachment_tools = AttachmentTools(store, sessions, brains, config.attachments, history)
        inline = InlineAttachments(store, config.attachments)
        orphans = OrphanSweeper(store, sessions)
    agent = Agent(
        model,
        history,
        ToolCallLog(sessions),
        ai=config.ai,
        window=config.history,
        memory=config.memory,
        users=config.users,
        toolsets=_build_toolsets(
            config,
            http_connector(),
            brains=brains,
            web=web,
            memory=search,
            facts=FactTools(facts, config.memory),
            reminders=reminders,
            attachments=attachment_tools,
        ),
        brains=brains,
        attachments=inline,
        facts=facts,
    )
    worker = Worker(
        queue,
        agent,
        history,
        outbox,
        config.queue,
        users=config.users,
        nudge=summaries.changed.set,
        typing=outbox.typing,
        kinds=kinds,
        parking=Parking(
            inputs,
            outbox,
            endings=endings,
            expire_after=timedelta(minutes=config.input_requests.expire_after_min),
        ),
    )
    dispatcher = make_dispatcher(
        config.users,
        chat,
        intake=intake,
        album_wait_s=config.attachments.album_wait_s if config.attachments else 2,
        memory=MemoryCommand(facts, ZoneInfo(config.ai.timezone)),
        on_tap=Taps(config.users, inputs, endings, outbox, queue.activity.notify),
    )
    await register_commands(bot)
    server = _Server(
        uvicorn.Config(
            create_app(config.services, config.users, queue, history, outbox),
            host=config.server.host,
            port=config.server.port,
            log_level=config.server.log_level.lower(),
            proxy_headers=True,
            lifespan="off",
        )
    )

    # Only with [history] idle_close_after_h.
    idle: IdleCloser | None = None
    if idle_after is not None:
        idle = IdleCloser(history, idle_after, busy=busy, nudge=summaries.changed.set)
    return _Services(
        server=server,
        sessions=sessions,
        bot=bot,
        dispatcher=dispatcher,
        worker=worker,
        sweeper=sweeper,
        queue=queue,
        reminders=reminders,
        inputs=inputs,
        endings=endings,
        outbox=outbox,
        brains=brains,
        orphans=orphans,
        idle=idle,
    )


async def _run_tasks(config: Config, services: _Services) -> asyncio.Task[object] | None:
    """Run every task until SIGTERM or SIGINT, or until one stops on its own (returned)."""
    s = services
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    tasks = {
        asyncio.create_task(s.server.serve(), name="api"),
        asyncio.create_task(
            s.dispatcher.start_polling(
                s.bot,
                handle_signals=False,
                close_bot_session=False,
                allowed_updates=ALLOWED_UPDATES,
            ),
            name="telegram",
        ),
        asyncio.create_task(s.worker.run_forever(), name="worker"),
        asyncio.create_task(s.sweeper.run_forever(), name="memory"),
        asyncio.create_task(
            run_due_loop(s.sessions, s.reminders.store, s.queue, config.reminders.check_interval_s),
            name="reminders",
        ),
        # On the reminders' clock; no setting of its own.
        asyncio.create_task(
            run_expiry_loop(
                s.inputs, s.endings, s.outbox, config.users, config.reminders.check_interval_s
            ),
            name="input-requests",
        ),
    }
    if s.orphans is not None:
        tasks.add(asyncio.create_task(s.orphans.run_forever(), name="attachments"))
    if s.idle is not None:
        tasks.add(asyncio.create_task(s.idle.run_forever(), name="idle"))
    stopper = asyncio.create_task(stop.wait(), name="stop")
    failed: asyncio.Task[object] | None = None
    try:
        done, _ = await asyncio.wait(tasks | {stopper}, return_when=asyncio.FIRST_COMPLETED)
        if stopper not in done:
            failed = next(iter(done))
            log.error("%s stopped unexpectedly; shutting down", failed.get_name())
        else:
            log.info("shutting down")
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        stopper.cancel()
        s.server.should_exit = True
        with contextlib.suppress(RuntimeError):  # polling may already be stopped
            await s.dispatcher.stop_polling()
        for task in tasks:
            if task.get_name() in _CANCELLED_ON_STOP:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return failed


def _log_startup(config: Config, brains: Brains) -> None:
    log.info(
        "alfred started: API on %s:%s, brain %s (of %s) at %s, summaries by %s, "
        "embeddings by %s at %s, web search at %s, reminders checked every %gs, "
        "attachments %s, idle close %s, %d MCP server(s), users %s (admin %s)",
        config.server.host,
        config.server.port,
        brains.current.name,
        ", ".join(f"{b.name}={b.tag}" for b in brains.brains),
        config.ai.base_url,
        config.memory.summary_model_or(config.ai.model),
        config.memory.embedding_model,
        config.memory.embedding_base_url,
        config.web.searxng_url,
        config.reminders.check_interval_s,
        (
            f"in bucket {config.attachments.bucket} at {config.attachments.endpoint_url} "
            f"(vision: {', '.join(b.name for b in brains.brains if b.vision) or 'none'})"
            if config.attachments
            else "off"
        ),
        (
            f"after {config.history.idle_close_after_h:g}h"
            if config.history.idle_close_after_h is not None
            else "off"
        ),
        len(config.mcp_servers),
        ", ".join(config.users.names),
        config.users.admin.name,
    )
