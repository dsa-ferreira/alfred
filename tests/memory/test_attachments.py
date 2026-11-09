"""Attachment handles in summaries and recalls (change add-attachments, design D11)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.history import History
from alfred.memory.summaries import Summaries
from alfred.memory.transcript import TranscriptEntry, render
from tests.helpers import FakeEmbedder, FakeModel, make_config, owner_with_files, pdf_file, text
from tests.memory.test_recall import memory

SECRET_PAGE = "Account IBAN PT50 0002 0123 1234 5678 9015 4"


async def archive_with_invoice(sessions: async_sessionmaker[AsyncSession]) -> int:
    history = History(sessions)
    await owner_with_files(sessions, "file this", [pdf_file([SECRET_PAGE, "page two"])])
    await history.append("assistant", "Filed, sir.")
    reset = await history.reset()
    assert reset.archived_id is not None
    return reset.archived_id


async def test_recall_shows_handles(sessions: async_sessionmaker[AsyncSession]) -> None:
    cid = await archive_with_invoice(sessions)
    result = await memory(sessions).recall(cid)
    assert result.ok
    lines = result.text.splitlines()
    assert lines[1:] == [
        'Owner: file this [attachment #1: PDF "invoice.pdf", 2 pages, text]',
        "alfred: Filed, sir.",
    ]
    assert SECRET_PAGE not in result.text


async def test_summary_transcript_has_no_pdf_text(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    cid = await archive_with_invoice(sessions)
    model = FakeModel([text("Title: Invoice filed\nSummary: An invoice was filed.")])
    summaries = Summaries(make_config(), sessions, model, FakeEmbedder(default=[1.0]))
    assert await summaries.summarise(cid) == "Invoice filed"
    transcript = model.requests[0][0][1]["content"]
    assert 'Owner: file this [attachment #1: PDF "invoice.pdf", 2 pages, text]' in transcript
    assert SECRET_PAGE not in transcript and "page two" not in transcript


def test_owner_line_without_caption() -> None:
    from alfred.attachments.handles import AttachmentRef

    photo = AttachmentRef(3, "image", "image/jpeg", "attachments/x")
    assert render([TranscriptEntry("owner", None, "", attachments=(photo,))]) == (
        "Owner: [attachment #3: image]"
    )
