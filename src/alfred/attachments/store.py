"""Where attachment files are kept: an S3-compatible bucket (design D1, D10).

:class:`ObjectStore` is what the rest of alfred uses; :class:`S3Store` is
the aiobotocore implementation, with one client for alfred's lifetime. Every
failure is raised as :class:`StoreError`, whose message names the operation,
the key and the error code, never the credentials.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from botocore.exceptions import BotoCoreError, ClientError

from alfred.config import AttachmentsConfig, S3Key

log = logging.getLogger(__name__)

PREFIX = "attachments/"
CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 60
MAX_ATTEMPTS = 3


class StoreError(Exception):
    """The object store refused or didn't answer. The message holds no secret."""


@dataclass(frozen=True)
class StoredObject:
    key: str
    last_modified: datetime


class ObjectStore(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    def list(self, prefix: str) -> AsyncIterator[StoredObject]:
        """Every object under ``prefix``, page by page."""
        ...

    async def delete(self, key: str) -> None: ...


class S3Store:
    """An :class:`ObjectStore` on one bucket, through one aiobotocore client."""

    def __init__(
        self,
        client: Any,
        bucket: str,
        close: contextlib.AsyncExitStack,
        *,
        page_size: int | None = None,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._close = close
        # Objects per list request; None is the server's (1000 on S3).
        self.page_size = page_size

    @classmethod
    async def open(cls, config: AttachmentsConfig, key: S3Key) -> S3Store:
        """Create the client; nothing is contacted until the first request."""
        stack = contextlib.AsyncExitStack()
        client = await stack.enter_async_context(
            get_session().create_client(
                "s3",
                endpoint_url=config.endpoint_url,
                region_name=config.region,
                aws_access_key_id=key.access_key_id.get_secret_value(),
                aws_secret_access_key=key.secret_access_key.get_secret_value(),
                config=AioConfig(
                    s3={"addressing_style": "path"},
                    # Newer default checksum headers that some S3-compatible
                    # servers (Garage among them) reject.
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                    connect_timeout=CONNECT_TIMEOUT_S,
                    read_timeout=READ_TIMEOUT_S,
                    retries={"mode": "standard", "total_max_attempts": MAX_ATTEMPTS},
                ),
            )
        )
        return cls(client, config.bucket, stack)

    async def aclose(self) -> None:
        await self._close.aclose()

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        async with self._errors("put", key):
            await self._client.put_object(
                Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
            )

    async def get(self, key: str) -> bytes:
        async with self._errors("get", key):
            response = await self._client.get_object(Bucket=self._bucket, Key=key)
            async with response["Body"] as body:
                return await body.read()

    async def list(self, prefix: str) -> AsyncIterator[StoredObject]:
        paginator = self._client.get_paginator("list_objects_v2")
        async with self._errors("list", prefix):
            pages = paginator.paginate(
                Bucket=self._bucket,
                Prefix=prefix,
                PaginationConfig={"PageSize": self.page_size} if self.page_size else {},
            )
            async for page in pages:
                for item in page.get("Contents", ()):
                    yield StoredObject(item["Key"], item["LastModified"])

    async def delete(self, key: str) -> None:
        async with self._errors("delete", key):
            await self._client.delete_object(Bucket=self._bucket, Key=key)

    @contextlib.asynccontextmanager
    async def _errors(self, operation: str, key: str) -> AsyncIterator[None]:
        try:
            yield
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "unknown")
            raise self._failed(operation, key, str(code)) from None
        except (TimeoutError, BotoCoreError, OSError) as exc:
            raise self._failed(operation, key, type(exc).__name__) from None

    def _failed(self, operation: str, key: str, code: str) -> StoreError:
        # Only the code, never the exception's text: no part of a request,
        # and so no credential, can reach the log.
        error = StoreError(f"object store {operation} {key} failed: {code}")
        log.warning("%s", error)
        return error
