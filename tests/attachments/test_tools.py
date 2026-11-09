"""``alfred_view_attachment`` and ``alfred_read_document`` (change add-attachments, design D8)."""

from __future__ import annotations

import io
import json

from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.brains import Brains
from alfred.agent.turn import Agent
from alfred.attachments.tools import (
    READ_DOCUMENT,
    TEXT_ONLY_NOTE,
    VIEW_ATTACHMENT,
    VISION_NOTE,
    AttachmentTools,
)
from alfred.config import AttachmentsConfig, BrainEntry
from alfred.history import History
from alfred.memory.search import ConversationSearch
from alfred.tools.base import AttemptContext, ToolOutcome, TurnInput
from tests.agent.turn_helpers import BAD_MODEL, GEMMA
from tests.attachments.files import image_bytes, scanned_pdf, text_pdf
from tests.helpers import (
    FakeEmbedder,
    FakeModel,
    FakeStore,
    calls,
    image_file,
    make_config,
    owner_with_files,
    pdf_file,
    text,
)

SETTINGS = AttachmentsConfig(endpoint_url="http://garage.lan:3900", bucket="alfred")
PAGES = [f"Invoice page {n}: lines and amounts for the plumbing work." for n in range(1, 13)]


def brains(vision: bool = True) -> Brains:
    return Brains(GEMMA, {"gemma": BrainEntry(model=GEMMA, vision=vision)})


def tools(
    sessions: async_sessionmaker[AsyncSession],
    store: FakeStore,
    the_brains: Brains | None = None,
    **settings: object,
) -> AttachmentTools:
    return AttachmentTools(
        store, sessions, the_brains or brains(), SETTINGS.model_copy(update=settings)
    )


async def call(toolset: AttachmentTools, name: str, **arguments: object) -> ToolOutcome:
    attempt = AttemptContext(TurnInput("owner", "x", 1))
    return await toolset.call(name, json.dumps(arguments), attempt)


async def invoice(sessions: async_sessionmaker[AsyncSession], store: FakeStore) -> None:
    sizes = [(612, 792)] * 12
    sizes[3] = (300, 600)  # page 4 is half-size, portrait
    data = text_pdf(PAGES, sizes=sizes)
    await owner_with_files(sessions, "file this", [pdf_file(PAGES)], store, [data])


def size(outcome: ToolOutcome) -> tuple[int, int]:
    (image,) = outcome.images
    return Image.open(io.BytesIO(image.data)).size


# --- offered ----------------------------------------------------------------------


async def test_owner_turn_on_a_vision_brain(sessions: async_sessionmaker[AsyncSession]) -> None:
    opened = await tools(sessions, FakeStore()).open(TurnInput("owner", "hi", 1))
    assert opened is not None
    assert [d["function"]["name"] for d in opened.definitions] == [VIEW_ATTACHMENT, READ_DOCUMENT]
    (note,) = opened.prompt_notes
    assert note.text == VISION_NOTE
    for words in ("[attachment #7", VIEW_ATTACHMENT, READ_DOCUMENT, "A handle alone does not"):
        assert words in note.text


async def test_text_only_brain(sessions: async_sessionmaker[AsyncSession]) -> None:
    opened = await tools(sessions, FakeStore(), brains(vision=False)).open(
        TurnInput("owner", "hi", 1)
    )
    assert opened is not None
    assert [d["function"]["name"] for d in opened.definitions] == [READ_DOCUMENT]
    (note,) = opened.prompt_notes
    assert note.text == TEXT_ONLY_NOTE
    assert VIEW_ATTACHMENT not in note.text and READ_DOCUMENT in note.text


async def test_not_offered_on_ask_and_reminder_turns(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    toolset = tools(sessions, FakeStore())
    assert await toolset.open(TurnInput("ask", "x", 1, "disk-check")) is None
    assert await toolset.open(TurnInput("reminder", "x", 1)) is None


# --- view -------------------------------------------------------------------------


async def test_look_again(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    await owner_with_files(
        sessions, "total?", [image_file(name="receipt.jpg")], store, [image_bytes(4000, 3000)]
    )
    await History(sessions).append("owner", "and the date on it?")
    outcome = await call(tools(sessions, store), VIEW_ATTACHMENT, id=1)
    assert outcome.ok
    assert outcome.text == '[attachment #1: image "receipt.jpg"]: the image, attached.'
    assert size(outcome) == (1536, 1152)


async def test_pdf_page(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    await invoice(sessions, store)
    outcome = await call(tools(sessions, store), VIEW_ATTACHMENT, id="#1", page=4)
    assert outcome.ok and "page 4 of 12" in outcome.text
    assert size(outcome) == (768, 1536)


async def test_page_out_of_range(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    await invoice(sessions, store)
    outcome = await call(tools(sessions, store), VIEW_ATTACHMENT, id=1, page=13)
    assert not outcome.ok
    assert outcome.text == "Error: attachment #1 is a PDF with 12 pages."
    assert store.gets == []


async def test_store_down_on_a_follow_up(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    await owner_with_files(sessions, "total?", [image_file()], store, [image_bytes()])
    store.failing = True
    outcome = await call(tools(sessions, store), VIEW_ATTACHMENT, id=1)
    assert not outcome.ok and outcome.images == ()
    assert "isn't available right now" in outcome.text


async def test_bad_id(sessions: async_sessionmaker[AsyncSession]) -> None:
    for value in (None, "receipt", 0, -1, True):
        outcome = await call(tools(sessions, FakeStore()), VIEW_ATTACHMENT, id=value)
        assert not outcome.ok and "attachment number" in outcome.text


async def test_view_refused_on_a_text_only_brain(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = FakeStore()
    await owner_with_files(sessions, "total?", [image_file()], store, [image_bytes()])
    outcome = await call(tools(sessions, store, brains(vision=False)), VIEW_ATTACHMENT, id=1)
    assert not outcome.ok and "can't see images" in outcome.text


# --- which attachments are reachable ------------------------------------------------


async def archived_with_invoice(
    sessions: async_sessionmaker[AsyncSession], store: FakeStore
) -> int:
    await invoice(sessions, store)
    reset = await History(sessions).reset()
    assert reset.archived_id is not None
    await History(sessions).append("owner", "about that invoice")
    return reset.archived_id


async def test_from_a_recalled_conversation(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    archived = await archived_with_invoice(sessions, store)
    recall = await ConversationSearch(make_config(), sessions, FakeEmbedder()).recall(archived)
    assert recall.ok and "[attachment #1: PDF" in recall.text
    outcome = await call(tools(sessions, store), READ_DOCUMENT, id=1, pages="2")
    assert outcome.ok and PAGES[1] in outcome.text


async def test_from_an_unrelated_conversation(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    await archived_with_invoice(sessions, store)
    toolset = tools(sessions, store)
    for name in (VIEW_ATTACHMENT, READ_DOCUMENT):
        outcome = await call(toolset, name, id=1)
        assert not outcome.ok
        assert outcome.text == "Error: there is no attachment #1 in this conversation."
    assert store.gets == []


async def test_recalled_inside_a_recalled_conversation_is_not_reachable(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = FakeStore()
    search = ConversationSearch(make_config(), sessions, FakeEmbedder())
    first = await archived_with_invoice(sessions, store)  # #1 has the invoice
    assert (await search.recall(first)).ok  # recalled into #2
    second = (await History(sessions).reset()).archived_id
    await History(sessions).append("owner", "hi")
    assert second is not None and (await search.recall(second)).ok  # #2 into #3
    outcome = await call(tools(sessions, store), READ_DOCUMENT, id=1)
    assert not outcome.ok and "no attachment #1" in outcome.text


# --- read -------------------------------------------------------------------------


async def test_read_pages(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    await invoice(sessions, store)
    outcome = await call(tools(sessions, store), READ_DOCUMENT, id=1, pages="3-5")
    assert outcome.ok
    assert outcome.text == "\n".join(
        [
            '[attachment #1: PDF "invoice.pdf", 12 pages, text]',
            f"--- page 3 ---\n{PAGES[2]}",
            f"--- page 4 ---\n{PAGES[3]}",
            f"--- page 5 ---\n{PAGES[4]}",
        ]
    )
    assert store.gets == []  # text comes from Postgres


async def test_read_from_page_one_by_default(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    await invoice(sessions, store)
    outcome = await call(tools(sessions, store), READ_DOCUMENT, id=1)
    assert outcome.text.count("--- page") == 12
    assert "continue" not in outcome.text


async def test_range_past_the_end_is_cut_to_the_last_page(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = FakeStore()
    await invoice(sessions, store)
    outcome = await call(tools(sessions, store), READ_DOCUMENT, id=1, pages="11-20")
    assert outcome.ok and outcome.text.count("--- page") == 2
    bad = await call(tools(sessions, store), READ_DOCUMENT, id=1, pages="13")
    assert bad.text == "Error: attachment #1 is a PDF with 12 pages."


async def test_bad_page_ranges(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    await invoice(sessions, store)
    for pages in ("five", "5-3", "0", "1,2"):
        outcome = await call(tools(sessions, store), READ_DOCUMENT, id=1, pages=pages)
        assert not outcome.ok and "pages must be" in outcome.text


async def test_long_range(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    await invoice(sessions, store)
    block = len(f"--- page 1 ---\n{PAGES[0]}")
    toolset = tools(sessions, store, read_chars=block * 3 + 5)
    outcome = await call(toolset, READ_DOCUMENT, id=1, pages="2-9")
    assert outcome.text.count("--- page") == 3
    assert outcome.text.endswith(f"--- page 4 ---\n{PAGES[3]}\n(continue from page 5)")


async def test_one_page_longer_than_the_limit(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    long = "word " * 400
    await owner_with_files(sessions, "", [pdf_file([long, "page two text, long enough"])], store)
    outcome = await call(tools(sessions, store, read_chars=100), READ_DOCUMENT, id=1)
    assert outcome.text.endswith("\n(page 1 cut at 100 characters)\n(continue from page 2)")


async def test_page_without_text(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    await owner_with_files(sessions, "", [pdf_file(["", "12", PAGES[0]])], store)
    vision = await call(tools(sessions, store), READ_DOCUMENT, id=1)
    assert "--- page 1 ---\n(no text on this page; use alfred_view_attachment)" in vision.text
    assert "--- page 2 ---\n12\n(no text on this page; use alfred_view_attachment)" in vision.text
    blind = await call(tools(sessions, store, brains(vision=False)), READ_DOCUMENT, id=1)
    assert "--- page 1 ---\n(no text on this page)\n" in blind.text
    assert VIEW_ATTACHMENT not in blind.text


async def test_not_a_pdf(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    await owner_with_files(sessions, "", [image_file()], store, [image_bytes()])
    outcome = await call(tools(sessions, store), READ_DOCUMENT, id=1)
    assert not outcome.ok
    assert outcome.text == (
        "Error: attachment #1 is an image, not a PDF. Look at it with alfred_view_attachment."
    )


async def test_larger_than_the_global_limit(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    pages = ["x" * 1990 for _ in range(3)]  # about 6000 characters
    await owner_with_files(sessions, "", [pdf_file(pages)], store)
    config = make_config(ai={"tool_result_max_chars": 4000}, mcp_servers=[])
    the_brains = brains()
    toolset = AttachmentTools(store, sessions, the_brains, SETTINGS)  # read_chars 8000
    model = FakeModel([calls((READ_DOCUMENT, '{"id": 1}')), text("Read.")])
    item = (await owner_with_files(sessions, "read it", []))[0]
    agent = Agent(config, model, History(sessions), sessions, toolsets=[toolset], brains=the_brains)
    await agent.run(TurnInput("owner", "read it", item))
    result = model.requests[1][0][-1]["content"]
    assert len(result) > 6000
    assert all(p in result for p in pages)


# --- fallback -----------------------------------------------------------------------


async def test_fallback_mid_turn(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    await owner_with_files(sessions, "", [pdf_file(["9"])], store, [scanned_pdf()])
    the_brains = Brains(
        GEMMA,
        {"gemma": GEMMA, "bonsai": BrainEntry(model="bonsai-2:27b", vision=True)},
    )
    the_brains.set("bonsai")
    toolset = AttachmentTools(store, sessions, the_brains, SETTINGS)
    model = FakeModel([BAD_MODEL, calls((VIEW_ATTACHMENT, '{"id": 1}')), text("Sorry, sir.")])
    item = (await owner_with_files(sessions, "what's on it?", []))[0]
    agent = Agent(
        make_config(), model, History(sessions), sessions, toolsets=[toolset], brains=the_brains
    )
    await agent.run(TurnInput("owner", "what's on it?", item))
    assert model.models[:2] == ["bonsai-2:27b", GEMMA]
    assert model.requests[2][0][-1]["content"] == (
        'Error: the current brain "gemma" can\'t see images.'
    )
    assert store.gets == []
