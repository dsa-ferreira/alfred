"""Receiving files: check, download, read, store, then queue in one transaction (design D2)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred import notices
from alfred.attachments.intake import DownloadFailed, IncomingFile, Intake
from alfred.config import AttachmentsConfig
from alfred.db.models import Attachment, AttachmentPage, QueueItem
from alfred.queue import Queue
from tests.attachments.files import image_bytes, text_pdf
from tests.helpers import FakeStore

CONFIG = AttachmentsConfig(endpoint_url="http://garage.lan:3900", bucket="alfred")
RECEIPT = image_bytes(800, 600)
INVOICE = text_pdf(["Invoice 2026-118, Lisbon Plumbing, 240.00 EUR", "Terms and conditions"])


class FakeDownloader:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.downloaded: list[str] = []

    async def download(self, file_id: str) -> bytes:
        self.downloaded.append(file_id)
        if file_id not in self.files:
            raise DownloadFailed(f"no file {file_id}")
        return self.files[file_id]


def intake(
    sessions: async_sessionmaker[AsyncSession],
    files: dict[str, bytes],
    store: FakeStore | None = None,
    config: AttachmentsConfig = CONFIG,
) -> tuple[Intake, FakeDownloader, FakeStore]:
    downloader = FakeDownloader(files)
    store = store or FakeStore()
    return (
        Intake(config, store, Queue(sessions), downloader, owner_address="sir"),
        downloader,
        store,
    )


async def rows(sessions: async_sessionmaker[AsyncSession]) -> tuple[list, list, list]:
    async with sessions() as session:
        items = list(await session.scalars(select(QueueItem).order_by(QueueItem.id)))
        attachments = list(await session.scalars(select(Attachment).order_by(Attachment.position)))
        pages = list(
            await session.scalars(
                select(AttachmentPage).order_by(AttachmentPage.attachment_id, AttachmentPage.page)
            )
        )
    return items, attachments, pages


async def test_photo_with_caption(sessions: async_sessionmaker[AsyncSession]) -> None:
    take, _, store = intake(sessions, {"p1": RECEIPT})
    received = await take.receive("what's the total?", [IncomingFile("image", "p1", 50_000)], 77)
    assert received.notices == []
    items, (attachment,), pages = await rows(sessions)
    assert [(i.id, i.kind, i.text, i.telegram_message_id) for i in items] == [
        (received.item_id, "owner", "what's the total?", 77)
    ]
    assert (attachment.queue_item_id, attachment.kind, attachment.mime) == (
        received.item_id,
        "image",
        "image/jpeg",
    )
    assert (attachment.width, attachment.height, attachment.size_bytes) == (800, 600, len(RECEIPT))
    assert attachment.entry_id is None and attachment.file_name is None
    assert attachment.object_key.startswith("attachments/")
    assert store.objects[attachment.object_key][:2] == (RECEIPT, "image/jpeg")
    assert pages == []


async def test_pdf_without_caption(sessions: async_sessionmaker[AsyncSession]) -> None:
    take, _, store = intake(sessions, {"d1": INVOICE})
    received = await take.receive("", [IncomingFile("pdf", "d1", len(INVOICE), "invoice.pdf")])
    (item,), (attachment,), pages = await rows(sessions)
    assert (item.id, item.text) == (received.item_id, "")
    assert (attachment.kind, attachment.mime, attachment.file_name, attachment.page_count) == (
        "pdf",
        "application/pdf",
        "invoice.pdf",
        2,
    )
    assert [(p.attachment_id, p.page, p.text) for p in pages] == [
        (attachment.id, 1, "Invoice 2026-118, Lisbon Plumbing, 240.00 EUR"),
        (attachment.id, 2, "Terms and conditions"),
    ]
    assert store.objects[attachment.object_key][0] == INVOICE


async def test_image_sent_as_a_file(sessions: async_sessionmaker[AsyncSession]) -> None:
    png = image_bytes(64, 48, "PNG")
    take, _, _ = intake(sessions, {"d1": png})
    await take.receive("", [IncomingFile("image", "d1", len(png), "rack.png")])
    _, (attachment,), _ = await rows(sessions)
    assert (attachment.kind, attachment.mime, attachment.file_name) == (
        "image",
        "image/png",
        "rack.png",
    )


async def test_too_large_is_not_downloaded(sessions: async_sessionmaker[AsyncSession]) -> None:
    take, downloader, store = intake(sessions, {"d1": INVOICE})
    received = await take.receive("", [IncomingFile("pdf", "d1", 30_000_000, "big.pdf")])
    assert received.item_id is None
    assert received.notices == [notices.too_large("sir", 20_000_000)]
    assert downloader.downloaded == []
    assert store.objects == {}
    assert await rows(sessions) == ([], [], [])


async def test_too_large_once_downloaded(sessions: async_sessionmaker[AsyncSession]) -> None:
    small = CONFIG.model_copy(update={"max_bytes": 1000})
    take, _, _ = intake(sessions, {"p1": RECEIPT}, config=small)
    received = await take.receive("hm", [IncomingFile("image", "p1", None)])
    assert received.notices == [notices.too_large("sir", 1000)]
    assert await rows(sessions) == ([], [], [])


async def test_password_protected_pdf(sessions: async_sessionmaker[AsyncSession]) -> None:
    locked = text_pdf(["Payslip November"], encrypt_password="hunter2")
    take, _, store = intake(sessions, {"d1": locked})
    received = await take.receive("", [IncomingFile("pdf", "d1", len(locked), "payslip.pdf")])
    assert received.item_id is None
    assert received.notices == [notices.unreadable("sir", "PDF")]
    assert store.objects == {}
    assert await rows(sessions) == ([], [], [])


async def test_image_that_cant_be_decoded(sessions: async_sessionmaker[AsyncSession]) -> None:
    take, _, _ = intake(sessions, {"p1": RECEIPT[:300]})
    received = await take.receive("", [IncomingFile("image", "p1", 300)])
    assert received.notices == [notices.unreadable("sir", "image")]
    assert await rows(sessions) == ([], [], [])


async def test_bucket_unreachable(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = FakeStore()
    store.failing = True
    take, _, _ = intake(sessions, {"p1": RECEIPT}, store=store)
    received = await take.receive("what's the total?", [IncomingFile("image", "p1", 1000)])
    assert received.item_id is None
    assert received.notices == [notices.not_kept("sir")]
    assert await rows(sessions) == ([], [], [])


async def test_download_failure(sessions: async_sessionmaker[AsyncSession]) -> None:
    take, _, store = intake(sessions, {"p1": RECEIPT})
    received = await take.receive(
        "", [IncomingFile("image", "p1", 1000), IncomingFile("image", "gone", 1000)]
    )
    assert received.notices == [notices.not_kept("sir")]
    assert store.objects == {}
    assert await rows(sessions) == ([], [], [])


async def test_refused_part_notified_and_the_rest_queued(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    take, _, store = intake(sessions, {"a": RECEIPT, "b": b"not an image", "c": INVOICE})
    received = await take.receive(
        "which is cheapest?",
        [IncomingFile("image", "a", 10), IncomingFile("image", "b", 10), IncomingFile("pdf", "c")],
    )
    assert received.notices == [notices.unreadable("sir", "image")]
    (item,), attachments, _ = await rows(sessions)
    assert item.text == "which is cheapest?"
    assert [(a.position, a.kind) for a in attachments] == [(0, "image"), (1, "pdf")]
    assert len(store.objects) == 2
