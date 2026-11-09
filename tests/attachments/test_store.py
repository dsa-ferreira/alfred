"""``S3Store`` against moto's S3 server (design D10)."""

from __future__ import annotations

import logging
import socket
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from alfred.attachments.store import PREFIX, S3Store, StoreError
from alfred.config import AttachmentsConfig, S3Key

SECRET = "LEAK-s3-secret-key"
KEY = S3Key(SecretStr("test-key-id"), SecretStr(SECRET))


def settings(endpoint: str, bucket: str) -> AttachmentsConfig:
    return AttachmentsConfig(endpoint_url=endpoint, bucket=bucket)


@pytest.fixture
async def store(s3_endpoint: str) -> AsyncIterator[S3Store]:
    bucket = f"alfred-{uuid.uuid4().hex[:12]}"
    store = await S3Store.open(settings(s3_endpoint, bucket), KEY)
    await store._client.create_bucket(
        Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "garage"}
    )
    try:
        yield store
    finally:
        await store.aclose()


async def test_put_get_list_delete(store: S3Store) -> None:
    before = datetime.now(UTC) - timedelta(minutes=1)
    await store.put(PREFIX + "a", b"\x89PNG one", "image/png")
    await store.put(PREFIX + "b", b"%PDF two", "application/pdf")
    await store.put("other/c", b"three", "text/plain")

    assert await store.get(PREFIX + "a") == b"\x89PNG one"
    listed = [o async for o in store.list(PREFIX)]
    assert sorted(o.key for o in listed) == [PREFIX + "a", PREFIX + "b"]
    assert all(o.last_modified >= before for o in listed)

    await store.delete(PREFIX + "a")
    assert [o.key async for o in store.list(PREFIX)] == [PREFIX + "b"]
    with pytest.raises(StoreError, match="NoSuchKey"):
        await store.get(PREFIX + "a")


async def test_list_pages_through_everything(store: S3Store) -> None:
    store.page_size = 2
    for i in range(5):
        await store.put(f"{PREFIX}{i}", b"x", "image/jpeg")
    assert [o.key async for o in store.list(PREFIX)] == [f"{PREFIX}{i}" for i in range(5)]


async def test_missing_bucket_is_logged_without_the_secret(
    s3_endpoint: str, caplog: pytest.LogCaptureFixture
) -> None:
    store = await S3Store.open(settings(s3_endpoint, "no-such-bucket"), KEY)
    try:
        with caplog.at_level(logging.DEBUG), pytest.raises(StoreError) as exc:
            await store.put(PREFIX + "a", b"x", "image/jpeg")
    finally:
        await store.aclose()
    assert "NoSuchBucket" in str(exc.value) and PREFIX + "a" in str(exc.value)
    assert "NoSuchBucket" in caplog.text
    assert SECRET not in caplog.text and SECRET not in str(exc.value)


async def test_unreachable_store_is_logged_without_the_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed again: nothing listens there
    store = await S3Store.open(settings(f"http://127.0.0.1:{port}", "alfred"), KEY)
    try:
        with caplog.at_level(logging.DEBUG), pytest.raises(StoreError) as exc:
            await store.get(PREFIX + "a")
    finally:
        await store.aclose()
    assert "get attachments/a failed" in str(exc.value)
    assert SECRET not in caplog.text and SECRET not in str(exc.value)
