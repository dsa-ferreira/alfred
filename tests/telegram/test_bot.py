from __future__ import annotations

import logging

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred import notices
from alfred.db.models import QueueItem
from alfred.queue import Queue
from alfred.telegram.bot import TelegramOutbox, make_dispatcher
from tests.telegram.fake_bot import OWNER, make_bot, message_update


async def queued(sessions: async_sessionmaker[AsyncSession]) -> list[QueueItem]:
    async with sessions() as session:
        return list(await session.scalars(select(QueueItem).order_by(QueueItem.id)))


# --- Owner-only access ---------------------------------------------------------


async def test_owner_message_is_queued(sessions: async_sessionmaker[AsyncSession]) -> None:
    bot, session = make_bot()
    dispatcher = make_dispatcher(OWNER, Queue(sessions), "sir")
    update = message_update(text="what's downloading?")
    await dispatcher.feed_update(bot, update)
    (item,) = await queued(sessions)
    assert (item.kind, item.text, item.telegram_message_id) == (
        "owner",
        "what's downloading?",
        update.message.message_id,
    )
    assert session.sent() == []


async def test_stranger_ignored(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    bot, session = make_bot()
    dispatcher = make_dispatcher(OWNER, Queue(sessions), "sir")
    with caplog.at_level(logging.INFO):
        await dispatcher.feed_update(bot, message_update(user_id=999, text="secret plans"))
    assert await queued(sessions) == []
    assert session.requests == []
    assert "999" in caplog.text
    assert "secret plans" not in caplog.text


async def test_group_chat_ignored(sessions: async_sessionmaker[AsyncSession]) -> None:
    bot, session = make_bot()
    dispatcher = make_dispatcher(OWNER, Queue(sessions), "sir")
    await dispatcher.feed_update(bot, message_update(chat_type="group", text="hello bot"))
    assert await queued(sessions) == []
    assert session.requests == []


# --- No bot commands -----------------------------------------------------------


async def test_slash_text_queued_as_written(sessions: async_sessionmaker[AsyncSession]) -> None:
    bot, _ = make_bot()
    dispatcher = make_dispatcher(OWNER, Queue(sessions), "sir")
    await dispatcher.feed_update(
        bot,
        message_update(text="/start", entities=[{"type": "bot_command", "offset": 0, "length": 6}]),
    )
    assert [i.text for i in await queued(sessions)] == ["/start"]


# --- Text only -----------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        {"voice": {"file_id": "v", "file_unique_id": "v", "duration": 3}},
        {"sticker": {"file_id": "s", "file_unique_id": "s", "type": "regular", "width": 1,
                     "height": 1, "is_animated": False, "is_video": False}},
        {"photo": [{"file_id": "p", "file_unique_id": "p", "width": 1, "height": 1}]},
    ],
    ids=["voice", "sticker", "photo"],
)  # fmt: skip
async def test_text_only(sessions: async_sessionmaker[AsyncSession], content: dict) -> None:
    bot, session = make_bot()
    dispatcher = make_dispatcher(OWNER, Queue(sessions), "sir")
    await dispatcher.feed_update(bot, message_update(**content))
    assert await queued(sessions) == []
    (sent,) = session.sent()
    assert sent.text == notices.text_only("sir") and sent.chat_id == OWNER


async def test_text_only_uses_configured_address(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    bot, session = make_bot()
    dispatcher = make_dispatcher(OWNER, Queue(sessions), "Mr. Ferreira")
    photo = [{"file_id": "p", "file_unique_id": "p", "width": 1, "height": 1}]
    await dispatcher.feed_update(bot, message_update(photo=photo))
    (sent,) = session.sent()
    assert sent.text.endswith(", Mr. Ferreira.")


# --- Sending -------------------------------------------------------------------


async def test_telegram_formatting() -> None:
    bot, session = make_bot()
    await TelegramOutbox(bot, OWNER).send("**disk full**\n```\ndf -h\n```")
    (sent,) = session.sent()
    assert sent.parse_mode == "HTML"
    assert sent.text == "<b>disk full</b>\n<pre>df -h</pre>"


async def test_formatting_rejected_falls_back_to_plain_text() -> None:
    bot, session = make_bot()

    def reject_html(method) -> None:  # type: ignore[no-untyped-def]
        if method.parse_mode == "HTML":
            raise TelegramBadRequest(method, "Bad Request: can't parse entities: whatever")

    session.on_request = reject_html
    await TelegramOutbox(bot, OWNER).send("**odd** text")
    html, plain = session.sent()
    assert html.parse_mode == "HTML"
    assert plain.parse_mode is None and plain.text == "**odd** text"


async def test_other_errors_propagate() -> None:
    bot, session = make_bot()

    def fail(method) -> None:  # type: ignore[no-untyped-def]
        raise TelegramNetworkError(method, "unreachable")

    session.on_request = fail
    with pytest.raises(TelegramNetworkError):
        await TelegramOutbox(bot, OWNER).send("hi")


async def test_long_replies_are_split() -> None:
    bot, session = make_bot()
    reply = "\n\n".join(f"Paragraph {i}: " + "word " * 80 for i in range(15))
    assert len(reply) > 6000
    await TelegramOutbox(bot, OWNER).send(reply)
    sent = session.sent()
    assert len(sent) >= 2
    assert all(len(m.text) <= 4096 for m in sent)
    assert [m.text.split(":")[0] for m in sent][0] == "Paragraph 0"
    assert " ".join(m.text for m in sent).split() == reply.split()


# --- Reply to every owner message ----------------------------------------------


async def test_reply_to_every_owner_message(sessions: async_sessionmaker[AsyncSession]) -> None:
    from alfred.agent.model import ModelFailed
    from alfred.agent.turn import Agent
    from alfred.history import History
    from alfred.queue import Worker
    from tests.helpers import FakeModel, make_config, text
    from tests.mcp.servers import in_process

    bot, session = make_bot()
    queue = Queue(sessions)
    dispatcher = make_dispatcher(OWNER, queue, "sir")
    model = FakeModel(
        [text("All good."), ModelFailed("model server answered HTTP 400: token=abc Traceback")]
    )
    cfg = make_config()
    history = History(sessions)
    agent = Agent(cfg, model, history, sessions, in_process({}))
    worker = Worker(
        queue,
        agent,
        history,
        TelegramOutbox(bot, OWNER),
        cfg.queue,
        owner_address=cfg.ai.owner_address,
    )

    await dispatcher.feed_update(bot, message_update(text="status?"))
    await dispatcher.feed_update(bot, message_update(text="and now?"))
    while await worker.step():
        pass

    first, second = session.sent()
    assert first.text == "All good." and first.chat_id == OWNER
    assert second.text == notices.failed("sir")
    assert "400" not in second.text and "token" not in second.text
