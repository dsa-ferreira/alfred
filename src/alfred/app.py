"""``alfred serve``: the bot, the service API and the AI worker in one event loop.

Three asyncio tasks share the database engine and the queue's in-process
signal (design D1). SIGTERM or SIGINT stops all three; a turn in progress is
interrupted and its item stays ``pending``, so it runs again after restart.
If any task stops on its own (for example the port is taken), the others are
stopped too and ``serve`` raises.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Generator

import uvicorn
from aiogram import Bot

from alfred.agent.model import ModelClient
from alfred.agent.turn import Agent
from alfred.api import create_app
from alfred.config import Config
from alfred.db.engine import make_engine, make_session_factory
from alfred.history import History
from alfred.mcp.client import http_connector
from alfred.queue import Queue, Worker
from alfred.telegram.bot import ALLOWED_UPDATES, TelegramOutbox, make_dispatcher

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


async def serve(config: Config) -> None:
    engine = make_engine(config.database_url.get_secret_value())
    sessions = make_session_factory(engine)
    history = History(sessions)
    queue = Queue(sessions)
    bot = Bot(config.telegram_bot_token.get_secret_value())
    outbox = TelegramOutbox(bot, config.telegram.owner_user_id)
    model = ModelClient(config.ai.base_url, config.ai.model, config.ai.request_timeout_s)
    agent = Agent(config, model, history, sessions, http_connector())
    worker = Worker(
        queue, agent, history, outbox, config.queue, owner_address=config.ai.owner_address
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
    }
    stopper = asyncio.create_task(stop.wait(), name="stop")
    log.info(
        "alfred started: API on %s:%s, model %s at %s, %d MCP server(s)",
        config.server.host,
        config.server.port,
        config.ai.model,
        config.ai.base_url,
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
            if task.get_name() == "worker":
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.session.close()
        await model.aclose()
        await engine.dispose()
    if failed is not None:
        exc = failed.exception() if not failed.cancelled() else None
        cause = f": {type(exc).__name__}: {exc}" if exc is not None else ""
        # Belt and braces: error texts never carry the bot token, but make sure.
        cause = cause.replace(config.telegram_bot_token.get_secret_value(), "<bot token>")
        raise RuntimeError(f"{failed.get_name()} stopped unexpectedly{cause}") from exc
    log.info("alfred stopped")
