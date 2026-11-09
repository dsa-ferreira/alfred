"""``alfred serve``: the bot, the service API, the AI worker, the memory sweeper
and the due-reminder loop.

Five asyncio tasks share the database engine and the queue's in-process
signal (design D1). SIGTERM or SIGINT stops all five; a turn in progress is
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
from collections.abc import Generator
from zoneinfo import ZoneInfo

import uvicorn
from aiogram import Bot

from alfred.agent.brains import Brains, BrainTools
from alfred.agent.conversation import ConversationTools
from alfred.agent.model import ModelClient
from alfred.agent.toolset import Toolset
from alfred.agent.turn import Agent
from alfred.api import create_app
from alfred.config import Config
from alfred.db.engine import make_engine, make_session_factory
from alfred.history import History
from alfred.mcp.client import Connector, http_connector
from alfred.mcp.toolset import McpTools
from alfred.memory.embedding import EmbeddingClient
from alfred.memory.search import ConversationSearch, MemoryTools, Searcher
from alfred.memory.summaries import Summaries
from alfred.memory.sweeper import Sweeper
from alfred.queue import Queue, Worker
from alfred.reminders.loop import run_due_loop
from alfred.reminders.tools import ReminderTools
from alfred.telegram.bot import ALLOWED_UPDATES, TelegramOutbox, make_dispatcher
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
    # Per-request lines from the HTTP clients are noise at INFO.
    for name in ("httpx", "httpx2", "aiogram.event"):
        logging.getLogger(name).setLevel(max(logging.WARNING, logging.getLogger().level))


def build_toolsets(
    config: Config,
    connect: Connector,
    *,
    brains: Brains,
    web: WebTools | None = None,
    memory: Searcher | None = None,
    reminders: ReminderTools | None = None,
) -> list[Toolset]:
    """What the model may call, in the order it is offered; alfred's own tools first.

    Each toolset decides on which turns it is offered. A name offered by two
    toolsets goes to the first.
    """
    toolsets: list[Toolset] = []
    if web is not None:
        toolsets.append(web)
    toolsets.append(ConversationTools())
    if memory is not None:
        toolsets.append(MemoryTools(memory))
    if reminders is not None:
        toolsets.append(reminders)
    toolsets.append(BrainTools(brains))
    toolsets.append(McpTools.from_config(config, connect))
    return toolsets


async def serve(config: Config) -> None:
    engine = make_engine(config.database_url.get_secret_value())
    sessions = make_session_factory(engine)
    history = History(sessions)
    queue = Queue(sessions)
    bot = Bot(config.telegram_bot_token.get_secret_value())
    outbox = TelegramOutbox(bot, config.telegram.owner_user_id)
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
    sweeper = Sweeper(config, sessions, summaries)
    search = ConversationSearch(config, sessions, embedder)
    # Turns pass the current brain's tag per request; summaries never do, so
    # they stay on the summary model (change add-brain-switching, design D2).
    brains = Brains(config.ai.model, config.ai.models)
    # Web search and fetch share one HTTP client (change add-web-search).
    web = WebTools.from_config(config)
    reminders = ReminderTools.from_config(config, sessions)
    agent = Agent(
        config,
        model,
        history,
        sessions,
        toolsets=build_toolsets(
            config, http_connector(), brains=brains, web=web, memory=search, reminders=reminders
        ),
        brains=brains,
    )
    worker = Worker(
        queue,
        agent,
        history,
        outbox,
        config.queue,
        owner_address=config.ai.owner_address,
        memory=summaries,
        typing=outbox.typing,
        reminders=reminders.store,
        zone=ZoneInfo(config.ai.timezone),
    )
    dispatcher = make_dispatcher(config.telegram.owner_user_id, queue, config.ai.owner_address)
    server = _Server(
        uvicorn.Config(
            create_app(config.services, queue, history, outbox),
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
            run_due_loop(reminders.store, queue.signal, config.reminders.check_interval_s),
            name="reminders",
        ),
    }
    stopper = asyncio.create_task(stop.wait(), name="stop")
    log.info(
        "alfred started: API on %s:%s, brain %s (of %s) at %s, summaries by %s, "
        "embeddings by %s at %s, web search at %s, reminders checked every %gs, "
        "%d MCP server(s)",
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
        len(config.mcp_servers),
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
            if task.get_name() in ("worker", "memory", "reminders"):
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.session.close()
        await model.aclose()
        if summary_model is not model:
            await summary_model.aclose()
        await embedder.aclose()
        await web.aclose()
        await engine.dispose()
    if failed is not None:
        exc = failed.exception() if not failed.cancelled() else None
        cause = f": {type(exc).__name__}: {exc}" if exc is not None else ""
        # Belt and braces: error texts never carry the bot token, but make sure.
        cause = cause.replace(config.telegram_bot_token.get_secret_value(), "<bot token>")
        raise RuntimeError(f"{failed.get_name()} stopped unexpectedly{cause}") from exc
    log.info("alfred stopped")
