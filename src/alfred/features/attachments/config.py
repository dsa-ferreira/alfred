"""``[attachments]``: where images and PDFs from users are kept, and how they are read."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from pydantic import Field, SecretStr, field_validator

from alfred.infra.settings import HttpUrlStr, PositiveFloat, PositiveInt, Section

# Telegram bots can't download files larger than this.
_TELEGRAM_MAX_BYTES = 20_000_000


class AttachmentsConfig(Section):
    """Images and PDFs from users.

    The section's presence turns attachments on; the object store key comes
    from ``ALFRED_S3_ACCESS_KEY_ID`` / ``ALFRED_S3_SECRET_ACCESS_KEY``.
    """

    endpoint_url: HttpUrlStr
    bucket: Annotated[str, Field(min_length=1)]
    region: Annotated[str, Field(min_length=1)] = "garage"
    max_bytes: Annotated[int, Field(gt=0)] = _TELEGRAM_MAX_BYTES
    # How many of the newest user messages carry their attachments inline.
    inline_turns: Annotated[int, Field(ge=0)] = 1
    first_page_chars: PositiveInt = 4000
    read_chars: PositiveInt = 8000
    image_max_px: PositiveInt = 1536
    album_wait_s: PositiveFloat = 2

    @field_validator("max_bytes")
    @classmethod
    def _within_telegram_limit(cls, value: int) -> int:
        if value > _TELEGRAM_MAX_BYTES:
            raise ValueError(
                f"must be at most {_TELEGRAM_MAX_BYTES} "
                "(20 MB, the largest file a bot can download)"
            )
        return value


@dataclass(frozen=True)
class S3Key:
    access_key_id: SecretStr
    secret_access_key: SecretStr
