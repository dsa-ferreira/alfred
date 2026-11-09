from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from aiogram import Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendChatAction
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred import notices
from alfred.attachments.intake import DownloadFailed, Intake
from alfred.config import AttachmentsConfig
from alfred.db.models import Attachment, QueueItem
from alfred.queue import Queue
from alfred.telegram import bot as bot_module
from alfred.telegram.bot import TelegramDownloader, TelegramOutbox, make_dispatcher
from tests.attachments.files import image_bytes, text_pdf
from tests.attachments.test_intake import FakeDownloader
from tests.helpers import FakeStore
from tests.telegram.fake_bot import OWNER, TOKEN, make_bot, message_update

PDF = "application/pdf"


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


# --- Text only, without attachments configured ------------------------------------


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


# --- Supported message types, with attachments (change add-attachments) ----------


def files_dispatcher(
    sessions: async_sessionmaker[AsyncSession], files: dict[str, bytes], store: FakeStore
) -> Dispatcher:
    intake = Intake(
        AttachmentsConfig(endpoint_url="http://garage.lan:3900", bucket="alfred"),
        store,
        Queue(sessions),
        FakeDownloader(files),
        owner_address="sir",
    )
    return make_dispatcher(OWNER, Queue(sessions), "sir", intake=intake, album_wait_s=0.05)


async def attachments_of(sessions: async_sessionmaker[AsyncSession]) -> list[Attachment]:
    async with sessions() as session:
        return list(
            await session.scalars(
                select(Attachment).order_by(Attachment.queue_item_id, Attachment.position)
            )
        )


def photo(file_id: str, **extra: Any) -> dict[str, Any]:
    sizes = [
        {"file_id": f"{file_id}-small", "file_unique_id": "s", "width": 90, "height": 60},
        {"file_id": file_id, "file_unique_id": file_id, "width": 800, "height": 600,
         "file_size": 5000},
    ]  # fmt: skip
    return {"photo": sizes, **extra}


def document(file_id: str, name: str, mime: str, **extra: Any) -> dict[str, Any]:
    return {
        "document": {
            "file_id": file_id,
            "file_unique_id": file_id,
            "file_name": name,
            "mime_type": mime,
            "file_size": 5000,
        },
        **extra,
    }


@pytest.mark.parametrize(
    "content",
    [
        {"voice": {"file_id": "v", "file_unique_id": "v", "duration": 3}},
        {"sticker": {"file_id": "s", "file_unique_id": "s", "type": "regular", "width": 1,
                     "height": 1, "is_animated": False, "is_video": False}},
        document("x", "budget.xlsx",
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ],
    ids=["voice", "sticker", "spreadsheet"],
)  # fmt: skip
async def test_unsupported_with_attachments(
    sessions: async_sessionmaker[AsyncSession], content: dict
) -> None:
    bot, session = make_bot()
    store = FakeStore()
    dispatcher = files_dispatcher(sessions, {"x": b"PK..."}, store)
    await dispatcher.feed_update(bot, message_update(**content))
    assert await queued(sessions) == []
    assert store.objects == {}
    (sent,) = session.sent()
    assert sent.text == notices.unsupported("sir")


async def test_photo_with_caption_is_queued(sessions: async_sessionmaker[AsyncSession]) -> None:
    bot, session = make_bot()
    dispatcher = files_dispatcher(sessions, {"p1": image_bytes(800, 600)}, FakeStore())
    update = message_update(**photo("p1", caption="what's the total?"))
    await dispatcher.feed_update(bot, update)
    (item,) = await queued(sessions)
    assert (item.text, item.telegram_message_id) == ("what's the total?", update.message.message_id)
    (attachment,) = await attachments_of(sessions)
    assert (attachment.kind, attachment.width) == ("image", 800)  # the largest size
    assert session.sent() == []


async def test_pdf_and_image_files_are_queued(sessions: async_sessionmaker[AsyncSession]) -> None:
    bot, _ = make_bot()
    files = {"d1": text_pdf(["Invoice 2026-118, total 240.00 EUR"]), "d2": image_bytes(fmt="PNG")}
    dispatcher = files_dispatcher(sessions, files, FakeStore())
    await dispatcher.feed_update(bot, message_update(**document("d1", "invoice.pdf", PDF)))
    await dispatcher.feed_update(bot, message_update(**document("d2", "rack.png", "image/png")))
    assert [i.text for i in await queued(sessions)] == ["", ""]
    assert [(a.kind, a.file_name) for a in await attachments_of(sessions)] == [
        ("pdf", "invoice.pdf"),
        ("image", "rack.png"),
    ]


async def test_refusal_notice_is_sent(sessions: async_sessionmaker[AsyncSession]) -> None:
    bot, session = make_bot()
    locked = text_pdf(["Payslip"], encrypt_password="hunter2")
    dispatcher = files_dispatcher(sessions, {"d1": locked}, FakeStore())
    await dispatcher.feed_update(bot, message_update(**document("d1", "payslip.pdf", PDF)))
    assert await queued(sessions) == []
    assert [m.text for m in session.sent()] == [notices.unreadable("sir", "PDF")]


async def test_three_photos_in_one_album(sessions: async_sessionmaker[AsyncSession]) -> None:
    bot, session = make_bot()
    files = {"a": image_bytes(100, 50), "b": image_bytes(200, 50), "c": image_bytes(300, 50)}
    dispatcher = files_dispatcher(sessions, files, FakeStore())
    parts = [
        message_update(**photo("a", media_group_id="g1")),
        message_update(**photo("b", media_group_id="g1", caption="which is cheapest?")),
        message_update(**photo("c", media_group_id="g1")),
    ]
    for update in (parts[2], parts[0], parts[1]):  # delivered out of order
        await dispatcher.feed_update(bot, update)
    assert await queued(sessions) == []  # still collecting
    await dispatcher["albums"].drain()

    (item,) = await queued(sessions)
    assert (item.text, item.telegram_message_id) == (
        "which is cheapest?",
        parts[0].message.message_id,
    )
    assert [(a.position, a.width) for a in await attachments_of(sessions)] == [
        (0, 100),
        (1, 200),
        (2, 300),
    ]
    assert session.sent() == []


async def test_album_with_a_refused_part(sessions: async_sessionmaker[AsyncSession]) -> None:
    bot, session = make_bot()
    files = {"a": image_bytes(100, 50), "b": b"not an image", "c": image_bytes(300, 50)}
    dispatcher = files_dispatcher(sessions, files, FakeStore())
    for file_id in "abc":
        caption = {"caption": "compare"} if file_id == "a" else {}
        await dispatcher.feed_update(
            bot, message_update(**photo(file_id, media_group_id="g2", **caption))
        )
    await dispatcher["albums"].drain()
    (item,) = await queued(sessions)
    assert item.text == "compare"
    assert [a.width for a in await attachments_of(sessions)] == [100, 300]
    assert [m.text for m in session.sent()] == [notices.unreadable("sir", "image")]


async def test_separate_albums_are_separate_messages(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    bot, _ = make_bot()
    files = {"a": image_bytes(), "b": image_bytes()}
    dispatcher = files_dispatcher(sessions, files, FakeStore())
    await dispatcher.feed_update(bot, message_update(**photo("a", media_group_id="g3")))
    await dispatcher.feed_update(bot, message_update(**photo("b", media_group_id="g4")))
    await dispatcher["albums"].drain()
    assert len(await queued(sessions)) == 2


async def test_text_is_still_queued_with_attachments(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    bot, _ = make_bot()
    dispatcher = files_dispatcher(sessions, {}, FakeStore())
    await dispatcher.feed_update(bot, message_update(text="hello"))
    assert [i.text for i in await queued(sessions)] == ["hello"]


async def test_downloader_reads_the_file() -> None:
    bot, session = make_bot()
    session.files["d1"] = b"%PDF" + b"x" * 2500
    assert await TelegramDownloader(bot).download("d1") == session.files["d1"]


async def test_failed_download_never_quotes_the_token() -> None:
    bot, session = make_bot()
    with pytest.raises(DownloadFailed) as exc:
        await TelegramDownloader(bot).download("gone")
    assert TOKEN in session.downloaded[0]  # the URL has it...
    assert TOKEN not in str(exc.value)  # ...the error doesn't


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
    from alfred.history import History
    from alfred.queue import Worker
    from tests.helpers import FakeModel, make_agent, make_config, text
    from tests.mcp.servers import in_process

    bot, session = make_bot()
    queue = Queue(sessions)
    dispatcher = make_dispatcher(OWNER, queue, "sir")
    model = FakeModel(
        [text("All good."), ModelFailed("model server answered HTTP 400: token=abc Traceback")]
    )
    cfg = make_config()
    history = History(sessions)
    agent = make_agent(cfg, model, history, sessions, in_process({}))
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


# --- Typing indicator ----------------------------------------------------------


def chat_actions(session) -> list[SendChatAction]:  # type: ignore[no-untyped-def]
    return [r for r in session.requests if isinstance(r, SendChatAction)]


async def test_typing_while_the_block_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot_module, "TYPING_INTERVAL_S", 0.01)
    bot, session = make_bot()
    async with TelegramOutbox(bot, OWNER).typing():
        await asyncio.sleep(0.05)
    actions = chat_actions(session)
    assert len(actions) >= 2  # resent while the block runs
    assert all(a.chat_id == OWNER and a.action == "typing" for a in actions)
    after = len(actions)
    await asyncio.sleep(0.05)
    assert len(chat_actions(session)) == after  # nothing once the block exits


async def test_typing_failure_is_logged_and_retried(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(bot_module, "TYPING_INTERVAL_S", 0.01)
    bot, session = make_bot()
    failures = 0

    def fail_twice(method) -> None:  # type: ignore[no-untyped-def]
        nonlocal failures
        if isinstance(method, SendChatAction) and failures < 2:
            failures += 1
            raise TelegramBadRequest(method, "Bad Request: chat not found")

    session.on_request = fail_twice
    with caplog.at_level(logging.DEBUG, logger="alfred.telegram.bot"):
        async with TelegramOutbox(bot, OWNER).typing():
            await asyncio.sleep(0.06)
    assert len(chat_actions(session)) >= 3  # kept going after the failures
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    debugs = [r for r in caplog.records if "typing indicator" in r.getMessage()]
    assert len(warnings) == 1 and "TelegramBadRequest" in warnings[0].getMessage()
    assert len(debugs) == 2


async def test_typing_lets_the_block_error_through() -> None:
    bot, _ = make_bot()
    with pytest.raises(ValueError, match="boom"):
        async with TelegramOutbox(bot, OWNER).typing():
            raise ValueError("boom")


async def test_typing_exit_never_raises_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot_module, "TYPING_INTERVAL_S", 0.01)
    bot, session = make_bot()

    def fail(method) -> None:  # type: ignore[no-untyped-def]
        raise TelegramNetworkError(method, "unreachable")

    session.on_request = fail
    async with TelegramOutbox(bot, OWNER).typing():
        await asyncio.sleep(0.03)
