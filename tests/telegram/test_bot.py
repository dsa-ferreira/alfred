from __future__ import annotations

import asyncio
import dataclasses
import html
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram import Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendChatAction
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred import notices
from alfred.attachments import notices as attachments_notices
from alfred.attachments.config import AttachmentsConfig
from alfred.attachments.intake import DownloadFailed, Intake
from alfred.config import QueueConfig, _MemoryConfig
from alfred.conversations.store import History
from alfred.db.models import Attachment, QueueItem
from alfred.facts import notices as facts_notices
from alfred.facts.store import FactStore
from alfred.inputs import render
from alfred.inputs.callbacks import Taps
from alfred.inputs.loop import cancel_on_new_message
from alfred.inputs.parking import Parking
from alfred.inputs.store import InputRequests
from alfred.kernel.access import _confirm_question
from alfred.kernel.questions import Option, Question
from alfred.kernel.users import Users
from alfred.queue import Queue
from alfred.telegram import bot as bot_module
from alfred.telegram.bot import TelegramDownloader, TelegramOutbox, make_dispatcher
from tests.attachments.files import image_bytes, text_pdf
from tests.attachments.test_intake import FakeDownloader
from tests.fakes import (
    NoInputRequests,
    NoMemoryCommand,
    NoQuestions,
    NoReminders,
    ignore,
    no_taps,
    no_typing,
)
from tests.helpers import ANA, ANA_ID, DIOGO, FakeStore, add_queue_item
from tests.telegram.fake_bot import OWNER, TOKEN, callback_update, make_bot, message_update

PDF = "application/pdf"
USERS = Users((DIOGO, ANA))


async def queued(sessions: async_sessionmaker[AsyncSession]) -> list[QueueItem]:
    async with sessions() as session:
        return list(await session.scalars(select(QueueItem).order_by(QueueItem.id)))


# --- Owner-only access ---------------------------------------------------------


async def test_owner_message_is_queued(sessions: async_sessionmaker[AsyncSession]) -> None:
    bot, session = make_bot()
    dispatcher = make_dispatcher(
        USERS, Queue(sessions), intake=None, memory=NoMemoryCommand(), on_tap=no_taps
    )
    update = message_update(text="what's downloading?")
    await dispatcher.feed_update(bot, update)
    (item,) = await queued(sessions)
    assert (item.kind, item.user_name, item.text, item.telegram_message_id) == (
        "user",
        "diogo",
        "what's downloading?",
        update.message.message_id,
    )
    assert session.sent() == []


async def test_stranger_ignored(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    bot, session = make_bot()
    dispatcher = make_dispatcher(
        USERS, Queue(sessions), intake=None, memory=NoMemoryCommand(), on_tap=no_taps
    )
    with caplog.at_level(logging.INFO):
        await dispatcher.feed_update(bot, message_update(user_id=999, text="secret plans"))
    assert await queued(sessions) == []
    assert session.requests == []
    assert "999" in caplog.text
    assert "secret plans" not in caplog.text


async def test_group_chat_ignored(sessions: async_sessionmaker[AsyncSession]) -> None:
    bot, session = make_bot()
    dispatcher = make_dispatcher(
        USERS, Queue(sessions), intake=None, memory=NoMemoryCommand(), on_tap=no_taps
    )
    await dispatcher.feed_update(bot, message_update(chat_type="group", text="hello bot"))
    assert await queued(sessions) == []
    assert session.requests == []


# --- No bot commands -----------------------------------------------------------


async def test_slash_text_queued_as_written(sessions: async_sessionmaker[AsyncSession]) -> None:
    bot, _ = make_bot()
    dispatcher = make_dispatcher(
        USERS, Queue(sessions), intake=None, memory=NoMemoryCommand(), on_tap=no_taps
    )
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
    dispatcher = make_dispatcher(
        USERS, Queue(sessions), intake=None, memory=NoMemoryCommand(), on_tap=no_taps
    )
    await dispatcher.feed_update(bot, message_update(**content))
    assert await queued(sessions) == []
    (sent,) = session.sent()
    assert sent.text == notices.text_only("sir") and sent.chat_id == OWNER


async def test_text_only_uses_configured_address(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    bot, session = make_bot()
    dispatcher = make_dispatcher(
        Users((dataclasses.replace(DIOGO, address="Mr. Ferreira"),)),
        Queue(sessions),
        intake=None,
        memory=NoMemoryCommand(),
        on_tap=no_taps,
    )
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
    )
    return make_dispatcher(
        USERS,
        Queue(sessions),
        intake=intake,
        album_wait_s=0.05,
        memory=NoMemoryCommand(),
        on_tap=no_taps,
    )


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
    assert [m.text for m in session.sent()] == [attachments_notices.unreadable("sir", "PDF")]


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
    await dispatcher["albums"]._drain()

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
    await dispatcher["albums"]._drain()
    (item,) = await queued(sessions)
    assert item.text == "compare"
    assert [a.width for a in await attachments_of(sessions)] == [100, 300]
    assert [m.text for m in session.sent()] == [attachments_notices.unreadable("sir", "image")]


async def test_separate_albums_are_separate_messages(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    bot, _ = make_bot()
    files = {"a": image_bytes(), "b": image_bytes()}
    dispatcher = files_dispatcher(sessions, files, FakeStore())
    await dispatcher.feed_update(bot, message_update(**photo("a", media_group_id="g3")))
    await dispatcher.feed_update(bot, message_update(**photo("b", media_group_id="g4")))
    await dispatcher["albums"]._drain()
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
    await TelegramOutbox(bot).send(DIOGO, "**disk full**\n```\ndf -h\n```")
    (sent,) = session.sent()
    assert sent.parse_mode == "HTML"
    assert sent.text == "<b>disk full</b>\n<pre>df -h</pre>"


async def test_formatting_rejected_falls_back_to_plain_text() -> None:
    bot, session = make_bot()

    def reject_html(method) -> None:  # type: ignore[no-untyped-def]
        if method.parse_mode == "HTML":
            raise TelegramBadRequest(method, "Bad Request: can't parse entities: whatever")

    session.on_request = reject_html
    await TelegramOutbox(bot).send(DIOGO, "**odd** text")
    html, plain = session.sent()
    assert html.parse_mode == "HTML"
    assert plain.parse_mode is None and plain.text == "**odd** text"


async def test_other_errors_propagate() -> None:
    bot, session = make_bot()

    def fail(method) -> None:  # type: ignore[no-untyped-def]
        raise TelegramNetworkError(method, "unreachable")

    session.on_request = fail
    with pytest.raises(TelegramNetworkError):
        await TelegramOutbox(bot).send(DIOGO, "hi")


async def test_long_replies_are_split() -> None:
    bot, session = make_bot()
    reply = "\n\n".join(f"Paragraph {i}: " + "word " * 80 for i in range(15))
    assert len(reply) > 6000
    await TelegramOutbox(bot).send(DIOGO, reply)
    sent = session.sent()
    assert len(sent) >= 2
    assert all(len(m.text) <= 4096 for m in sent)
    assert [m.text.split(":")[0] for m in sent][0] == "Paragraph 0"
    assert " ".join(m.text for m in sent).split() == reply.split()


# --- Reply to every owner message ----------------------------------------------


async def test_reply_to_every_owner_message(sessions: async_sessionmaker[AsyncSession]) -> None:
    from alfred.conversations.store import History
    from alfred.llm.chat import ModelFailed
    from alfred.worker import Worker
    from tests.helpers import FakeModel, make_agent, make_config, text
    from tests.mcp.servers import in_process

    bot, session = make_bot()
    queue = Queue(sessions)
    dispatcher = make_dispatcher(
        USERS, queue, intake=None, memory=NoMemoryCommand(), on_tap=no_taps
    )
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
        TelegramOutbox(bot),
        cfg.queue,
        users=cfg.users,
        parking=Parking(NoInputRequests(), NoQuestions()),
        reminders=NoReminders(),
        typing=no_typing,
        nudge=ignore,
    )

    await dispatcher.feed_update(bot, message_update(text="status?"))
    await dispatcher.feed_update(bot, message_update(text="and now?"))
    while await worker._step():
        pass

    first, second = session.sent()
    assert first.text == "All good." and first.chat_id == OWNER
    assert second.text == notices.failed("sir")
    assert "400" not in second.text and "token" not in second.text


# --- Typing indicator ----------------------------------------------------------


def chat_actions(session) -> list[SendChatAction]:  # type: ignore[no-untyped-def]
    return [r for r in session.requests if isinstance(r, SendChatAction)]


async def test_typing_while_the_block_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot_module, "_TYPING_INTERVAL_S", 0.01)
    bot, session = make_bot()
    async with TelegramOutbox(bot).typing(DIOGO):
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
    monkeypatch.setattr(bot_module, "_TYPING_INTERVAL_S", 0.01)
    bot, session = make_bot()
    failures = 0

    def fail_twice(method) -> None:  # type: ignore[no-untyped-def]
        nonlocal failures
        if isinstance(method, SendChatAction) and failures < 2:
            failures += 1
            raise TelegramBadRequest(method, "Bad Request: chat not found")

    session.on_request = fail_twice
    with caplog.at_level(logging.DEBUG, logger="alfred.telegram.bot"):
        async with TelegramOutbox(bot).typing(DIOGO):
            await asyncio.sleep(0.06)
    assert len(chat_actions(session)) >= 3  # kept going after the failures
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    debugs = [r for r in caplog.records if "typing indicator" in r.getMessage()]
    assert len(warnings) == 1 and "TelegramBadRequest" in warnings[0].getMessage()
    assert len(debugs) == 2


async def test_typing_lets_the_block_error_through() -> None:
    bot, _ = make_bot()
    with pytest.raises(ValueError, match="boom"):
        async with TelegramOutbox(bot).typing(DIOGO):
            raise ValueError("boom")


async def test_typing_exit_never_raises_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot_module, "_TYPING_INTERVAL_S", 0.01)
    bot, session = make_bot()

    def fail(method) -> None:  # type: ignore[no-untyped-def]
        raise TelegramNetworkError(method, "unreachable")

    session.on_request = fail
    async with TelegramOutbox(bot).typing(DIOGO):
        await asyncio.sleep(0.03)


# --- Configured users (change add-multiple-users) -----------------------------------


async def test_second_user_message_is_queued_as_hers(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    bot, session = make_bot()
    dispatcher = make_dispatcher(
        USERS, Queue(sessions), intake=None, memory=NoMemoryCommand(), on_tap=no_taps
    )
    await dispatcher.feed_update(bot, message_update(user_id=ANA_ID, text="what's for dinner?"))
    (item,) = await queued(sessions)
    assert (item.kind, item.user_name, item.text) == ("user", "ana", "what's for dinner?")
    assert session.sent() == []


async def test_second_user_in_a_group_is_ignored(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    bot, session = make_bot()
    dispatcher = make_dispatcher(
        USERS, Queue(sessions), intake=None, memory=NoMemoryCommand(), on_tap=no_taps
    )
    await dispatcher.feed_update(bot, message_update(user_id=ANA_ID, chat_type="group", text="x"))
    assert await queued(sessions) == [] and session.requests == []


async def test_text_only_notice_in_her_chat_and_address(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    bot, session = make_bot()
    dispatcher = make_dispatcher(
        USERS, Queue(sessions), intake=None, memory=NoMemoryCommand(), on_tap=no_taps
    )
    sticker = {"file_id": "s", "file_unique_id": "s", "type": "regular"}
    sticker |= {"width": 1, "height": 1, "is_animated": False, "is_video": False}
    await dispatcher.feed_update(bot, message_update(user_id=ANA_ID, sticker=sticker))
    (sent,) = session.sent()
    assert sent.chat_id == ANA_ID and sent.text == notices.text_only("madam")


async def test_second_user_photo_and_album_are_hers(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    bot, session = make_bot()
    files = {f"p{i}": image_bytes(80, 60) for i in range(3)}
    dispatcher = files_dispatcher(sessions, files, FakeStore())
    await dispatcher.feed_update(bot, message_update(user_id=ANA_ID, **photo("p0", caption="a")))
    for i in (1, 2):
        await dispatcher.feed_update(
            bot, message_update(user_id=ANA_ID, media_group_id="g", **photo(f"p{i}"))
        )
    await dispatcher["albums"]._drain()
    items = await queued(sessions)
    assert [i.user_name for i in items] == ["ana", "ana"]
    assert {a.user_name for a in await attachments_of(sessions)} == {"ana"}
    assert session.sent() == []


async def test_refusal_notice_uses_her_address(sessions: async_sessionmaker[AsyncSession]) -> None:
    bot, session = make_bot()
    dispatcher = files_dispatcher(sessions, {}, FakeStore())
    voice = {"file_id": "v", "file_unique_id": "v", "duration": 1}
    await dispatcher.feed_update(bot, message_update(user_id=ANA_ID, voice=voice))
    (sent,) = session.sent()
    assert sent.chat_id == ANA_ID and sent.text == notices.unsupported("madam")


async def test_outbox_sends_to_the_users_chat() -> None:
    bot, session = make_bot()
    await TelegramOutbox(bot).send(ANA, "Dune is ready.")
    (sent,) = session.sent()
    assert sent.chat_id == ANA_ID


async def test_typing_in_the_users_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot_module, "_TYPING_INTERVAL_S", 0.01)
    bot, session = make_bot()
    async with TelegramOutbox(bot).typing(ANA):
        await asyncio.sleep(0.03)
    actions = chat_actions(session)
    assert actions and all(a.chat_id == ANA_ID for a in actions)


# --- /memory (change add-user-facts, design D11) ---------------------------------------


def memory_dispatcher(
    sessions: async_sessionmaker[AsyncSession],
) -> tuple[Dispatcher, FactStore]:
    from zoneinfo import ZoneInfo

    from alfred.telegram.bot import MemoryCommand
    from tests.helpers import FakeEmbedder

    store = FactStore(_MemoryConfig(), QueueConfig(), sessions, FakeEmbedder(default=[1.0]))
    memory = MemoryCommand(store, ZoneInfo("Europe/Lisbon"))
    return make_dispatcher(
        USERS, Queue(sessions), memory=memory, intake=None, on_tap=no_taps
    ), store


async def memory_texts(
    sessions: async_sessionmaker[AsyncSession],
    dispatcher: Dispatcher,
    text: str,
    user_id: int = OWNER,
) -> list[str]:
    bot, session = make_bot()
    await dispatcher.feed_update(bot, message_update(user_id=user_id, text=text))
    assert await queued(sessions) == []
    sent = session.sent()
    assert all(m.parse_mode is None and int(m.chat_id) == user_id for m in sent)
    return [m.text for m in sent]


@pytest.mark.parametrize("command", ["/memory", "/memory@alfred_bot", " /memory "])
async def test_memory_lists_the_users_facts(
    sessions: async_sessionmaker[AsyncSession], command: str
) -> None:
    dispatcher, store = memory_dispatcher(sessions)
    await store.add("diogo", "allergic to shellfish", "told")
    await store.add("diogo", "runs a homelab", "learned")
    await store.add("ana", "likes tea", "told")
    [text] = await memory_texts(sessions, dispatcher, command)
    lines = text.splitlines()
    assert lines[0] == facts_notices.memory_list("sir")
    assert [line.rsplit(", ", 1)[0] for line in lines[1:]] == [
        "[1] allergic to shellfish (told",
        "[2] runs a homelab (learned",
    ]
    assert "tea" not in text


async def test_memory_line_has_the_date(sessions: async_sessionmaker[AsyncSession]) -> None:
    from datetime import UTC, datetime

    from alfred.telegram.bot import MemoryCommand

    class OneFact:
        async def list(self, user_name: str) -> list[Any]:
            from alfred.facts.store import _FactRow

            return [
                _FactRow(12, "allergic to shellfish", "told", datetime(2026, 11, 3, tzinfo=UTC))
            ]

        async def forget(self, user_name: str, ids: Any) -> Any:
            raise AssertionError

    from zoneinfo import ZoneInfo

    texts = await MemoryCommand(OneFact(), ZoneInfo("Europe/Lisbon")).answer(DIOGO, None)
    assert texts[0].splitlines()[1] == "[12] allergic to shellfish (told, 3 Nov)"


@pytest.mark.parametrize("text", ["/memorybank ideas", "/start", "remember /memory"])
async def test_other_slash_text_still_queued(
    sessions: async_sessionmaker[AsyncSession], text: str
) -> None:
    dispatcher, _ = memory_dispatcher(sessions)
    bot, session = make_bot()
    await dispatcher.feed_update(bot, message_update(text=text))
    assert [i.text for i in await queued(sessions)] == [text]
    assert session.sent() == []


async def test_memory_forget(sessions: async_sessionmaker[AsyncSession]) -> None:
    dispatcher, store = memory_dispatcher(sessions)
    await store.add("diogo", "allergic to shellfish", "told")
    await store.add("diogo", "runs a homelab", "learned")
    await store.add("diogo", "likes jazz", "learned")
    [text] = await memory_texts(sessions, dispatcher, "/memory forget 1 2")
    lines = text.splitlines()
    assert lines[0] == "I have forgotten these, sir:"
    assert lines[1].startswith("[1] allergic to shellfish (told, ")
    assert lines[2].startswith("[2] runs a homelab (learned, ")
    assert [f.text for f in await store.list("diogo")] == ["likes jazz"]


async def test_memory_forget_of_another_users_fact(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    dispatcher, store = memory_dispatcher(sessions)
    await store.add("diogo", "allergic to shellfish", "told")
    texts = await memory_texts(sessions, dispatcher, "/memory forget 1", user_id=ANA_ID)
    assert texts == ["I found no fact 1, madam."]
    assert len(await store.list("diogo")) == 1


async def test_memory_forget_some_not_found(sessions: async_sessionmaker[AsyncSession]) -> None:
    dispatcher, store = memory_dispatcher(sessions)
    await store.add("diogo", "allergic to shellfish", "told")
    [text] = await memory_texts(sessions, dispatcher, "/memory forget #1, 99")
    assert text.splitlines()[-1] == "I found no fact 99."


@pytest.mark.parametrize("text", ["/memory clear", "/memory forget", "/memory forget all"])
async def test_memory_usage(sessions: async_sessionmaker[AsyncSession], text: str) -> None:
    dispatcher, store = memory_dispatcher(sessions)
    await store.add("diogo", "allergic to shellfish", "told")
    assert await memory_texts(sessions, dispatcher, text) == [facts_notices.memory_usage("sir")]
    assert len(await store.list("diogo")) == 1


async def test_memory_with_no_facts(sessions: async_sessionmaker[AsyncSession]) -> None:
    dispatcher, _ = memory_dispatcher(sessions)
    texts = await memory_texts(sessions, dispatcher, "/memory", user_id=ANA_ID)
    assert texts == ["I know no facts about you yet, madam."]


async def test_long_memory_list_is_split(sessions: async_sessionmaker[AsyncSession]) -> None:
    dispatcher, store = memory_dispatcher(sessions)
    for i in range(40):
        await store.add("diogo", f"fact {i:02} " + "x" * 200, "learned")
    texts = await memory_texts(sessions, dispatcher, "/memory")
    assert len(texts) > 1
    assert all(len(t) <= 4096 for t in texts)
    assert sum(t.count("fact ") for t in texts) == 40


async def test_memory_is_not_recorded(sessions: async_sessionmaker[AsyncSession]) -> None:
    from alfred.db.models import Entry

    dispatcher, store = memory_dispatcher(sessions)
    await store.add("diogo", "allergic to shellfish", "told")
    await memory_texts(sessions, dispatcher, "/memory")
    await memory_texts(sessions, dispatcher, "/memory forget 1")
    async with sessions() as session:
        assert list(await session.scalars(select(Entry))) == []


async def test_commands_registered() -> None:
    from aiogram.methods import SetMyCommands

    from alfred.telegram.bot import register_commands

    bot, session = make_bot()
    await register_commands(bot)
    [request] = [r for r in session.requests if isinstance(r, SetMyCommands)]
    assert [c.command for c in request.commands] == ["memory"]


async def test_command_registration_failure_is_only_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from alfred.telegram.bot import register_commands

    bot, session = make_bot()

    def fail(method: Any) -> None:
        raise TelegramNetworkError(method=method, message="down")

    session.on_request = fail
    await register_commands(bot)
    assert "could not register the bot commands" in caplog.text


# --- Questions and buttons (change add-user-input-requests) --------------------------

DUNE = _confirm_question("mcparr_add_media", {"title": "Dune", "year": 2021})


def titled(question: Question, title_: str = "For your request:") -> Question:
    return dataclasses.replace(question, title=title_)


async def test_confirmation_shows_the_real_call() -> None:
    bot, session = make_bot()
    message_id = await TelegramOutbox(bot).send_question(ANA, 7, titled(DUNE))
    (sent,) = session.sent()
    assert sent.chat_id == ANA_ID and sent.parse_mode == "HTML"
    assert message_id == 1
    assert sent.text == (
        "<b>For your request:</b>\n"
        "Run mcparr_add_media with these arguments?\n"
        "<pre>{\n  &quot;title&quot;: &quot;Dune&quot;,\n  &quot;year&quot;: 2021\n}</pre>"
    )
    rows = sent.reply_markup.inline_keyboard  # type: ignore[union-attr]
    assert [[(b.text, b.callback_data) for b in row] for row in rows] == [
        [("Do it", "ir:7:0"), ("Cancel", "ir:7:1")]
    ]


def test_titles_say_what_asked() -> None:
    assert render.title("user") == "For your request:"
    assert render.title("ask", source="jellyfin") == "For a request from jellyfin:"
    label = render.reminder_label(12, "call mum")
    assert render.title("reminder", reminder=label) == "For reminder #12 (call mum):"


async def test_request_from_a_service() -> None:
    bot, session = make_bot()
    question = titled(DUNE, render.title("ask", source="jellyfin"))
    await TelegramOutbox(bot).send_question(DIOGO, 3, question)
    assert "jellyfin" in session.sent()[0].text


async def test_long_arguments_are_cut_and_the_buttons_still_sent() -> None:
    bot, session = make_bot()
    long = _confirm_question("cookbook_create_recipe", {"steps": "x" * 10_000})
    await TelegramOutbox(bot).send_question(DIOGO, 4, titled(long))
    (sent,) = session.sent()
    visible = html.unescape(re.sub(r"<[^>]+>", "", sent.text))
    assert len(visible) <= 4096 - render._OUTCOME_RESERVE
    assert "more characters]" in visible
    assert sent.reply_markup is not None
    # With the outcome line added, the edit still fits.
    edited = html.unescape(re.sub(r"<[^>]+>", "", render.outcome_html(titled(long), "Confirmed.")))
    assert len(edited) <= 4096


def test_at_most_two_buttons_a_row() -> None:
    options = tuple(Option(str(i), i) for i in range(5))
    markup = render.keyboard(9, Question("Pick", options))
    assert [len(row) for row in markup.inline_keyboard] == [2, 2, 1]
    assert markup.inline_keyboard[2][0].callback_data == "ir:9:4"
    assert render.parse_callback("ir:9:4") == (9, 4)
    assert render.parse_callback("ir:x:1") is None and render.parse_callback(None) is None


async def test_edit_removes_the_buttons() -> None:
    bot, session = make_bot()
    await TelegramOutbox(bot).edit_question(ANA, 55, "done")
    (edit,) = [r for r in session.requests if isinstance(r, EditMessageText)]
    assert (edit.chat_id, edit.message_id, edit.text) == (ANA_ID, 55, "done")
    assert edit.reply_markup is None


async def test_failed_edit_is_only_logged(caplog: pytest.LogCaptureFixture) -> None:
    bot, session = make_bot()

    def refuse(method: object) -> None:
        if isinstance(method, EditMessageText):
            raise TelegramBadRequest(method, "message to edit not found")  # type: ignore[arg-type]

    session.on_request = refuse
    with caplog.at_level(logging.WARNING):
        await TelegramOutbox(bot).edit_question(ANA, 55, "done")
    assert "could not edit question message 55" in caplog.text


def test_callback_queries_are_received() -> None:
    assert bot_module.ALLOWED_UPDATES == ["message", "callback_query"]


# --- taps ----------------------------------------------------------------------


class Wakes:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


async def tap_setup(
    sessions: async_sessionmaker[AsyncSession], *, expires_at: datetime | None = None
) -> tuple[Any, Any, Dispatcher, InputRequests, int, int, Wakes]:
    bot, session = make_bot()
    requests = InputRequests(sessions, History(sessions), USERS)
    wakes = Wakes()
    taps = Taps(USERS, requests, TelegramOutbox(bot), wakes)
    dispatcher = make_dispatcher(
        USERS, Queue(sessions), on_tap=taps, intake=None, memory=NoMemoryCommand()
    )
    item = await add_queue_item(sessions, "user", "add Dune", user="ana")
    request = await requests.park(
        item, "ana", tool="mcparr_add_media", arguments={"title": "Dune"},
        question=titled(DUNE), state={"via": "confirm"}, turn={},
        expires_at=expires_at or datetime.now(UTC) + timedelta(hours=1),
    )  # fmt: skip
    await requests.set_message(request, "ana", 500)
    return bot, session, dispatcher, requests, item, request, wakes


def answers(session: Any) -> list[AnswerCallbackQuery]:
    return [r for r in session.requests if isinstance(r, AnswerCallbackQuery)]


def edits(session: Any) -> list[EditMessageText]:
    return [r for r in session.requests if isinstance(r, EditMessageText)]


async def item_status(sessions: async_sessionmaker[AsyncSession], item: int) -> str:
    async with sessions() as session:
        row = await session.get(QueueItem, item)
        assert row is not None
        return row.status


async def test_tap_acknowledged_and_applied(sessions: async_sessionmaker[AsyncSession]) -> None:
    bot, session, dispatcher, _, item, request, wakes = await tap_setup(sessions)
    await dispatcher.feed_update(bot, callback_update(f"ir:{request}:0", user_id=ANA_ID))
    (answer,) = answers(session)
    assert answer.text == "Do it"
    assert await item_status(sessions, item) == "pending"
    assert wakes.count == 1  # the worker is woken
    (edit,) = edits(session)
    assert edit.message_id == 500 and edit.text.endswith("<i>Confirmed.</i>")
    assert edit.reply_markup is None


async def test_tap_from_a_stranger(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    bot, session, dispatcher, _, item, request, wakes = await tap_setup(sessions)
    with caplog.at_level(logging.INFO):
        await dispatcher.feed_update(bot, callback_update(f"ir:{request}:0", user_id=999))
    assert len(answers(session)) == 1 and edits(session) == []
    assert await item_status(sessions, item) == "waiting" and wakes.count == 0
    assert "999" in caplog.text and f"ir:{request}" not in caplog.text


async def test_tap_in_a_group_is_ignored(sessions: async_sessionmaker[AsyncSession]) -> None:
    bot, session, dispatcher, _, item, request, _ = await tap_setup(sessions)
    update = callback_update(f"ir:{request}:0", user_id=ANA_ID, chat_type="group")
    await dispatcher.feed_update(bot, update)
    assert await item_status(sessions, item) == "waiting"


async def test_someone_else_taps(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    bot, session, dispatcher, _, item, request, wakes = await tap_setup(sessions)
    with caplog.at_level(logging.INFO):
        await dispatcher.feed_update(bot, callback_update(f"ir:{request}:0", user_id=OWNER))
    assert answers(session)[0].text == "That question isn't yours to answer."
    assert await item_status(sessions, item) == "waiting" and wakes.count == 0
    assert str(OWNER) in caplog.text


async def test_double_tap(sessions: async_sessionmaker[AsyncSession]) -> None:
    bot, session, dispatcher, _, item, request, wakes = await tap_setup(sessions)
    await dispatcher.feed_update(bot, callback_update(f"ir:{request}:0", user_id=ANA_ID))
    await dispatcher.feed_update(bot, callback_update(f"ir:{request}:1", user_id=ANA_ID))
    assert [a.text for a in answers(session)] == ["Do it", "That question is no longer open."]
    assert wakes.count == 1 and len(edits(session)) == 1


async def test_tap_on_an_old_message(sessions: async_sessionmaker[AsyncSession]) -> None:
    yesterday = datetime.now(UTC) - timedelta(days=1)
    bot, session, dispatcher, _, item, request, wakes = await tap_setup(
        sessions, expires_at=yesterday
    )
    await dispatcher.feed_update(bot, callback_update(f"ir:{request}:0", user_id=ANA_ID))
    assert answers(session)[0].text == "That question is no longer open."
    assert await item_status(sessions, item) == "cancelled" and wakes.count == 0
    (edit,) = edits(session)
    assert edit.text.endswith("<i>Expired: no answer in time.</i>")


async def test_tap_with_bad_data(sessions: async_sessionmaker[AsyncSession]) -> None:
    bot, session, dispatcher, *_ = await tap_setup(sessions)
    await dispatcher.feed_update(bot, callback_update("nonsense", user_id=ANA_ID))
    assert answers(session)[0].text == "That question is no longer open."


# --- a new message cancels waiting turns -----------------------------------------


async def waiting_for_diogo(
    sessions: async_sessionmaker[AsyncSession], bot: Any
) -> tuple[Queue, InputRequests, int]:
    """A queue wired as serve wires it, and diogo's parked turn: (queue, store, item id)."""
    requests = InputRequests(sessions, History(sessions), USERS)
    queue = Queue(sessions)
    queue.before_user_message = cancel_on_new_message(requests, TelegramOutbox(bot), USERS)
    item = await add_queue_item(sessions, "user", "add Dune")
    request = await requests.park(
        item, "diogo", tool="mcparr_add_media", arguments={"title": "Dune"},
        question=titled(DUNE), state={"via": "confirm"}, turn={},
        expires_at=timedelta(hours=1),
    )  # fmt: skip
    await requests.set_message(request, "diogo", 700)
    return queue, requests, item


async def test_text_message_cancels_the_waiting_turn(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    bot, session = make_bot()
    queue, _, item = await waiting_for_diogo(sessions, bot)
    dispatcher = make_dispatcher(
        USERS, queue, intake=None, memory=NoMemoryCommand(), on_tap=no_taps
    )
    await dispatcher.feed_update(bot, message_update(text="never mind"))
    assert await item_status(sessions, item) == "cancelled"
    (edit,) = edits(session)
    assert edit.message_id == 700
    assert edit.text.endswith("<i>Cancelled: you sent a new message.</i>")
    assert [i.text for i in await queued(sessions)] == ["add Dune", "never mind"]


async def test_attachment_message_cancels_the_waiting_turn(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    bot, _ = make_bot()
    queue, _, item = await waiting_for_diogo(sessions, bot)
    intake = Intake(
        AttachmentsConfig(endpoint_url="http://garage.lan:3900", bucket="alfred"),
        FakeStore(),
        queue,
        FakeDownloader({"p1": image_bytes(800, 600)}),
    )
    dispatcher = make_dispatcher(
        USERS, queue, intake=intake, memory=NoMemoryCommand(), on_tap=no_taps
    )
    await dispatcher.feed_update(bot, message_update(**photo("p1", caption="this instead")))
    assert await item_status(sessions, item) == "cancelled"


async def test_memory_command_leaves_the_turn_waiting(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    from zoneinfo import ZoneInfo

    from alfred.telegram.bot import MemoryCommand
    from tests.helpers import FakeEmbedder

    bot, session = make_bot()
    queue, requests, item = await waiting_for_diogo(sessions, bot)
    store = FactStore(_MemoryConfig(), QueueConfig(), sessions, FakeEmbedder(default=[1.0]))
    dispatcher = make_dispatcher(
        USERS,
        queue,
        memory=MemoryCommand(store, ZoneInfo("Europe/Lisbon")),
        intake=None,
        on_tap=no_taps,
    )
    await dispatcher.feed_update(bot, message_update(text="/memory"))
    assert await item_status(sessions, item) == "waiting"
    assert len(await requests._open_for("diogo")) == 1 and edits(session) == []


async def test_another_users_message_leaves_the_turn_waiting(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    bot, _ = make_bot()
    queue, _, item = await waiting_for_diogo(sessions, bot)
    await make_dispatcher(
        USERS, queue, intake=None, memory=NoMemoryCommand(), on_tap=no_taps
    ).feed_update(bot, message_update(user_id=ANA_ID, text="hello"))
    assert await item_status(sessions, item) == "waiting"
