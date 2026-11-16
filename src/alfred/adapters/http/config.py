"""``[server]`` and the ``[[services]]`` that may call the API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, SecretStr

from alfred.infra.settings import Section


class ServerConfig(Section):
    host: Annotated[str, Field(min_length=1)] = "0.0.0.0"
    port: Annotated[int, Field(ge=1, le=65535)] = 8080
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


@dataclass(frozen=True)
class Service:
    name: str
    token: SecretStr
