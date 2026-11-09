"""Attachments inline on the turn they arrive; handles later (change add-attachments, D4)."""

from __future__ import annotations

import base64
import io
import logging

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.brains import Brains
from alfred.agent.turn import Agent
from alfred.attachments.inline import InlineAttachments
from alfred.config import AttachmentsConfig, BrainEntry
from alfred.history import History, strip_stamp
from alfred.tools.base import TurnInput
from tests.agent.turn_helpers import BAD_MODEL, GEMMA
from tests.attachments.files import image_bytes, scanned_pdf, text_pdf
from tests.helpers import (
    FakeModel,
    FakeStore,
    image_file,
    make_config,
    owner_with_files,
    pdf_file,
    text,
)

SETTINGS = AttachmentsConfig(endpoint_url="http://garage.lan:3900", bucket="alfred")
RECEIPT = image_bytes(4000, 3000)
INVOICE_P1 = "Invoice 2026-118 from Lisbon Plumbing, total 240.00 EUR"


def brains(vision: bool = True, other_vision: bool = True) -> Brains:
    return Brains(
        GEMMA,
        {
            "gemma": BrainEntry(model=GEMMA, vision=vision),
            "bonsai": BrainEntry(model="bonsai:8b", vision=other_vision),
        },
    )


def make(
    sessions: async_sessionmaker[AsyncSession],
    model: FakeModel,
    store: FakeStore,
    *,
    vision: bool = True,
    the_brains: Brains | None = None,
    **settings: object,
) -> Agent:
    config = SETTINGS.model_copy(update=settings)
    return Agent(
        make_config(),
        model,
        History(sessions),
        sessions,
        brains=the_brains or brains(vision),
        attachments=InlineAttachments(store, config),
    )


def last_user(model: FakeModel, request: int = 0) -> dict:
    return [m for m in model.requests[request][0] if m["role"] == "user"][-1]


def image_size(part: dict) -> tuple[int, int]:
    data = part["image_url"]["url"].split(",", 1)[1]
    return Image.open(io.BytesIO(base64.b64decode(data))).size


def images_in(model: FakeModel, request: int = 0) -> int:
    return sum(
        1
        for m in model.requests[request][0]
        if isinstance(m["content"], list)
        for p in m["content"]
        if p["type"] == "image_url"
    )


async def test_photo_question(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    item, _ = await owner_with_files(
        sessions, "what's the total?", [image_file(name="receipt.jpg")], store, [RECEIPT]
    )
    model = FakeModel([text("12.40 EUR, sir.")])
    await make(sessions, model, store).run(TurnInput("owner", "what's the total?", item))
    text_part, image_part = last_user(model)["content"]
    assert strip_stamp(text_part["text"]) == (
        'what\'s the total? [attachment #1: image "receipt.jpg"]'
    )
    assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert image_size(image_part) == (1536, 1152)
    assert store.objects["attachments/photo"][0] == RECEIPT  # the stored file is unchanged


async def test_text_pdf(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    pages = [INVOICE_P1, *(f"page {n} body text here" for n in range(2, 13))]
    item, _ = await owner_with_files(sessions, "", [pdf_file(pages)], store, [text_pdf(pages)])
    model = FakeModel([text("Filed.")])
    await make(sessions, model, store).run(TurnInput("owner", "", item))
    content = last_user(model)["content"]
    assert isinstance(content, str)  # no image part
    assert strip_stamp(content) == (
        '[attachment #1: PDF "invoice.pdf", 12 pages, text]\n--- page 1 ---\n' + INVOICE_P1
    )
    assert store.gets == []  # page 1's text comes from Postgres


async def test_long_first_page_is_cut(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    long = "word " * 100
    item, _ = await owner_with_files(sessions, "", [pdf_file([long])], store, [b"%PDF"])
    model = FakeModel([text("ok")])
    await make(sessions, model, store, first_page_chars=50).run(TurnInput("owner", "", item))
    content = strip_stamp(last_user(model)["content"])
    assert content.endswith(
        "--- page 1 ---\n" + long.strip()[:50]
        + "\n(page 1 cut at 50 characters; read on with alfred_read_document)"
    )  # fmt: skip


async def test_scanned_pdf(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    item, _ = await owner_with_files(
        sessions, "", [pdf_file(["3"], name="letter.pdf")], store, [scanned_pdf(1, (600, 800))]
    )
    model = FakeModel([text("A letter.")])
    await make(sessions, model, store).run(TurnInput("owner", "", item))
    text_part, image_part = last_user(model)["content"]
    assert strip_stamp(text_part["text"]) == '[attachment #1: PDF "letter.pdf", 1 page, scanned]'
    assert image_size(image_part) == (1152, 1536)


async def test_follow_up_question(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    history = History(sessions)
    await owner_with_files(sessions, "what's the total?", [image_file()], store, [RECEIPT])
    await history.append("assistant", "12.40 EUR, sir.")
    item, _ = await owner_with_files(sessions, "and the date on it?", [])
    model = FakeModel([text("9 November.")])
    await make(sessions, model, store).run(TurnInput("owner", "and the date on it?", item))
    users = [strip_stamp(m["content"]) for m in model.requests[0][0] if m["role"] == "user"]
    assert users == ["what's the total? [attachment #1: image]", "and the date on it?"]
    assert images_in(model) == 0
    assert store.gets == []


async def test_inline_window_of_two(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    await owner_with_files(sessions, "first", [image_file()], store, [RECEIPT])
    item, _ = await owner_with_files(
        sessions, "and this?", [image_file("attachments/b")], store, [RECEIPT]
    )
    model = FakeModel([text("ok")])
    await make(sessions, model, store, inline_turns=2).run(TurnInput("owner", "and this?", item))
    assert images_in(model) == 2


async def test_photo_on_a_text_only_brain(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    item, _ = await owner_with_files(sessions, "what is this?", [image_file()], store, [RECEIPT])
    model = FakeModel([text("I can't see it, sir.")])
    await make(sessions, model, store, vision=False).run(TurnInput("owner", "what is this?", item))
    assert strip_stamp(last_user(model)["content"]) == (
        "what is this? [attachment #1: image (this brain can't see images)]"
    )
    assert images_in(model) == 0 and store.gets == []


async def test_pdf_on_a_text_only_brain(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    item, _ = await owner_with_files(sessions, "", [pdf_file([INVOICE_P1])], store, [b"%PDF"])
    model = FakeModel([text("ok")])
    await make(sessions, model, store, vision=False).run(TurnInput("owner", "", item))
    assert strip_stamp(last_user(model)["content"]) == (
        '[attachment #1: PDF "invoice.pdf", 1 page, text]\n--- page 1 ---\n' + INVOICE_P1
    )


async def test_store_failure_keeps_the_turn_going(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    store = FakeStore()
    item, _ = await owner_with_files(sessions, "total?", [image_file()], store, [RECEIPT])
    store.failing = True
    model = FakeModel([text("I can't see it right now, sir.")])
    with caplog.at_level(logging.WARNING):
        result = await make(sessions, model, store).run(TurnInput("owner", "total?", item))
    assert result.reply == "I can't see it right now, sir."
    assert strip_stamp(last_user(model)["content"]) == (
        "total? [attachment #1: image (not available right now)]"
    )
    assert "attachment #1 is not available" in caplog.text


async def test_fallback_attempt_reuses_the_images(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = FakeStore()
    item, _ = await owner_with_files(sessions, "total?", [image_file()], store, [RECEIPT])
    the_brains = brains()
    the_brains.set("bonsai")
    model = FakeModel([BAD_MODEL, text("12.40 EUR.")])
    await make(sessions, model, store, the_brains=the_brains).run(
        TurnInput("owner", "total?", item)
    )
    assert [images_in(model, n) for n in (0, 1)] == [1, 1]
    assert store.gets == ["attachments/photo"]  # fetched once for both attempts


async def test_fallback_to_a_text_only_brain_sends_no_images(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = FakeStore()
    item, _ = await owner_with_files(sessions, "total?", [image_file()], store, [RECEIPT])
    the_brains = brains(vision=False, other_vision=True)
    the_brains.set("bonsai")
    model = FakeModel([BAD_MODEL, text("I can't see it.")])
    await make(sessions, model, store, the_brains=the_brains).run(
        TurnInput("owner", "total?", item)
    )
    assert [images_in(model, n) for n in (0, 1)] == [1, 0]
    assert "(this brain can't see images)" in last_user(model, 1)["content"]


async def test_no_images_on_ask_turns(sessions: async_sessionmaker[AsyncSession]) -> None:
    from tests.helpers import add_queue_item

    store = FakeStore()
    await owner_with_files(sessions, "total?", [image_file()], store, [RECEIPT])
    item = await add_queue_item(sessions, "ask", "93% full", "disk-check")
    model = FakeModel([text("NO_REPLY")])
    await make(sessions, model, store).run(TurnInput("ask", "93% full", item, "disk-check"))
    assert images_in(model) == 0
