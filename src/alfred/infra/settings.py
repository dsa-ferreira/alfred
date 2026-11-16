"""What every config section is built on: the section base, the shared validators
and :class:`ConfigError`.

Each feature defines its own section in its ``config.py`` from these; the
composed configuration and its loader live in ``alfred.config``.
"""

from __future__ import annotations

import re
from typing import Annotated
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

# The variables secrets may be read from.
TOKEN_ENV = re.compile(r"ALFRED_[A-Z0-9_]+")
_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")


def _check_name(value: str) -> str:
    if not _NAME.fullmatch(value):
        raise ValueError("must be lowercase letters, digits and hyphens")
    return value


Name = Annotated[str, AfterValidator(_check_name)]


def _check_http_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("must be an http:// or https:// URL with a host")
    return value.rstrip("/")


HttpUrlStr = Annotated[str, AfterValidator(_check_http_url)]


def _check_token_env(value: str) -> str:
    if not TOKEN_ENV.fullmatch(value):
        raise ValueError("must name an ALFRED_* environment variable (A-Z, 0-9, _)")
    return value


TokenEnv = Annotated[str, AfterValidator(_check_token_env)]


def _check_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("unknown timezone; use an IANA name such as Europe/Lisbon") from None
    return value


TimeZoneName = Annotated[str, AfterValidator(_check_timezone)]
PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]
NonNegativeFloat = Annotated[float, Field(ge=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class ConfigError(Exception):
    """Invalid configuration. ``problems`` are safe to print and log."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("invalid configuration:\n" + "\n".join(f"  - {p}" for p in problems))


class Section(BaseModel):
    """A config file table: unknown keys are errors, and values never change."""

    model_config = ConfigDict(extra="forbid", frozen=True)
