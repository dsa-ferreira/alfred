"""``alfred serve``: the bot, the service API, the AI worker, the memory sweeper,
the due-reminder loop, with ``[attachments]`` the orphan file sweeper, and with
``[history] idle_close_after_h`` the idle conversation closer.

Five asyncio tasks (more with those) share the database engine and the
queue's in-process signal (design D1). SIGTERM or SIGINT stops them all; a turn in progress is
interrupted and its item stays ``pending``, so it runs again after restart,
a summary in progress is simply made again later, and a reminder claim is
one transaction, so it either happened or happens again. If any task stops on
its own (for example the port is taken), the others are stopped too and
``serve`` raises.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import uvicorn
from aiogram import Bot

from alfred.agent.brains import Brains
from alfred.agent.model import ModelClient
from alfred.agent.turn import Agent
from alfred.api import create_app
from alfred.attachments.inline import InlineAttachments
from alfred.attachments.intake import Intake
from alfred.attachments.store import S3Store
from alfred.attachments.sweeper import OrphanSweeper
from alfred.attachments.tools import AttachmentTools
from alfred.config import Config
from alfred.db.engine import make_engine, make_session_factory
from alfred.db.users import warn_unknown_users
from alfred.history import History
from alfred.idle import IdleCloser
from alfred.mcp.client import Connector, http_connector
from alfred.mcp.toolset import McpTools
from alfred.memory.duplicates import FactDuplicateJob
from alfred.memory.embedding import EmbeddingClient
from alfred.memory.extract import FactExtractionJob
from alfred.memory.facts import FactEmbeddingJob, FactStore, FactTools
from alfred.memory.search import ConversationSearch, MemoryTools, Searcher
from alfred.memory.summaries import Summaries
from alfred.memory.sweeper import Sweeper
from alfred.queue import Queue, Worker
from alfred.reminders.loop import run_due_loop
from alfred.reminders.tools import ReminderTools
from alfred.telegram.bot import (
    ALLOWED_UPDATES,
    MemoryCommand,
    TelegramDownloader,
    TelegramOutbox,
    make_dispatcher,
    register_commands,
)
from alfred.tools.base import Toolset
from alfred.tools.brains import BrainTools
from alfred.tools.calculator import CalculatorTools
from alfred.tools.conversation import ConversationTools
from alfred.tools.dates import DateTools
from alfred.web.tools import WebTools

log = logging.getLogger(__name__)


class _Server(uvicorn.Server):
    """uvicorn without its own signal handlers; ``serve`` owns shutdown."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True
    )
    # Per-request lines from the HTTP and S3 clients are noise at INFO, and
    # botocore's DEBUG lines show signed request headers.
    for name in ("httpx", "httpx2", "aiogram.event", "botocore", "aiobotocore"):
        logging.getLogger(name).setLevel(max(logging.WARNING, logging.getLogger().level))


def build_toolsets(
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
    # Only with [attachments] (change add-attachments, design D8).
    if attachments is not None:
        toolsets.append(attachments)
    if reminders is not None:
        toolsets.append(reminders)
    toolsets.append(BrainTools(brains))
    # Arithmetic and dates, on every turn (change add-utility-tools, design D7).
    toolsets.append(CalculatorTools())
    toolsets.append(DateTools(ZoneInfo(config.ai.timezone), now=now))
    toolsets.append(McpTools.from_config(config, connect))
    return toolsets


async def serve(config: Config) -> None:
    engine = make_engine(config.database_url.get_secret_value())
    sessions = make_session_factory(engine)
    # A misspelt ALFRED_MIGRATE_ADMIN or a renamed user shows up here (design D9).
    await warn_unknown_users(sessions, config.users.names)
    queue = Queue(sessions)
    bot = Bot(config.telegram_bot_token.get_secret_value())
    outbox = TelegramOutbox(bot)
    model = ModelClient(config.ai.base_url, config.ai.model, config.ai.request_timeout_s)
    # A second client only when summaries use another model (memory design D5).
    summary_model = (
        model
        if config.summary_model == config.ai.model
        else ModelClient(config.ai.base_url, config.summary_model, config.ai.request_timeout_s)
    )
    embedder = EmbeddingClient(
        config.memory.embedding_base_url,
        config.memory.embedding_model,
        config.memory.embedding_timeout_s,
        document_prefix=config.memory.document_prefix,
        query_prefix=config.memory.query_prefix,
    )
    summaries = Summaries(config, sessions, summary_model, embedder)
    # A conversation with no user message is archived when something new arrives
    # after the idle close time; the sweeper then summarises it (change
    # add-recurring-reminders, design D7).
    idle_after = (
        timedelta(hours=config.history.idle_close_after_h)
        if config.history.idle_close_after_h is not None
        else None
    )
    history = History(sessions, idle_after=idle_after, on_archive=summaries.changed.set)
    # User facts (change add-user-facts): extraction after the summaries, then
    # embedding, which needs no quiet queue, then the duplicate check.
    facts = FactStore(config, sessions, embedder)
    sweeper = Sweeper(
        config,
        sessions,
        summaries,
        queue=queue,
        jobs=[
            FactExtractionJob(config, sessions, summary_model, facts),
            FactEmbeddingJob(facts),
            # Needs the model, so only while the queue is quiet (change fix-fact-removals).
            FactDuplicateJob(facts, summary_model),
        ],
    )
    search = ConversationSearch(config, sessions, embedder)
    # Turns pass the current brain's tag per request; summaries never do, so
    # they stay on the summary model (change add-brain-switching, design D2).
    brains = Brains.from_config(config.ai)
    # Web search and fetch share one HTTP client (change add-web-search).
    web = WebTools.from_config(config)
    reminders = ReminderTools.from_config(config, sessions)
    # Photos, image files and PDFs, only with [attachments] (change add-attachments).
    store: S3Store | None = None
    intake: Intake | None = None
    attachment_tools: AttachmentTools | None = None
    inline: InlineAttachments | None = None
    orphans: OrphanSweeper | None = None
    if config.attachments is not None:
        assert config.s3_key is not None
        store = await S3Store.open(config.attachments, config.s3_key)
        intake = Intake(config.attachments, store, queue, TelegramDownloader(bot))
        attachment_tools = AttachmentTools(store, sessions, brains, config.attachments)
        inline = InlineAttachments(store, config.attachments)
        orphans = OrphanSweeper(store, sessions)
    agent = Agent(
        config,
        model,
        history,
        sessions,
        toolsets=build_toolsets(
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
        reminders=reminders.store,
        zone=ZoneInfo(config.ai.timezone),
    )
    dispatcher = make_dispatcher(
        config.users,
        queue,
        intake=intake,
        album_wait_s=config.attachments.album_wait_s if config.attachments else 2,
        memory=MemoryCommand(facts, ZoneInfo(config.ai.timezone)),
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

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    tasks = {
        asyncio.create_task(server.serve(), name="api"),
        asyncio.create_task(
            dispatcher.start_polling(
                bot,
                handle_signals=False,
                close_bot_session=False,
                allowed_updates=ALLOWED_UPDATES,
            ),
            name="telegram",
        ),
        asyncio.create_task(worker.run_forever(), name="worker"),
        asyncio.create_task(sweeper.run_forever(), name="memory"),
        asyncio.create_task(
            run_due_loop(reminders.store, queue.activity.notify, config.reminders.check_interval_s),
            name="reminders",
        ),
    }
    if orphans is not None:
        tasks.add(asyncio.create_task(orphans.run_forever(), name="attachments"))
    # Only with [history] idle_close_after_h (change close-idle-conversations, D3).
    if config.history.idle_close_after_h is not None:
        assert idle_after is not None
        idle = IdleCloser(history, idle_after, nudge=summaries.changed.set)
        tasks.add(asyncio.create_task(idle.run_forever(), name="idle"))
    stopper = asyncio.create_task(stop.wait(), name="stop")
    log.info(
        "alfred started: API on %s:%s, brain %s (of %s) at %s, summaries by %s, "
        "embeddings by %s at %s, web search at %s, reminders checked every %gs, "
        "attachments %s, idle close %s, %d MCP server(s), users %s (admin %s)",
        config.server.host,
        config.server.port,
        brains.current.name,
        ", ".join(f"{b.name}={b.tag}" for b in brains.brains),
        config.ai.base_url,
        config.summary_model,
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
        server.should_exit = True
        with contextlib.suppress(RuntimeError):  # polling may already be stopped
            await dispatcher.stop_polling()
        for task in tasks:
            if task.get_name() in ("worker", "memory", "reminders", "attachments", "idle"):
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.session.close()
        await model.aclose()
        if summary_model is not model:
            await summary_model.aclose()
        await embedder.aclose()
        await web.aclose()
        if store is not None:
            await store.aclose()
        await engine.dispose()
    if failed is not None:
        exc = failed.exception() if not failed.cancelled() else None
        cause = f": {type(exc).__name__}: {exc}" if exc is not None else ""
        # Belt and braces: error texts never carry the bot token, but make sure.
        cause = cause.replace(config.telegram_bot_token.get_secret_value(), "<bot token>")
        raise RuntimeError(f"{failed.get_name()} stopped unexpectedly{cause}") from exc
    log.info("alfred stopped")
