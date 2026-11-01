"""Startup configuration: ``alfred.toml`` plus ``ALFRED_*`` secret variables.

Every non-secret setting lives in the TOML file (path from ``ALFRED_CONFIG``,
default ``alfred.toml``). Secrets are only read from environment variables:
``ALFRED_TELEGRAM_BOT_TOKEN``, ``ALFRED_DATABASE_URL`` and the variables named
by each service's and MCP server's ``token_env``.

Everything is validated once. All problems are collected into one
:class:`ConfigError` whose messages name keys and variables but never include
a secret value.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    model_validator,
)

CONFIG_ENV = "ALFRED_CONFIG"
DEFAULT_CONFIG_PATH = "alfred.toml"
BOT_TOKEN_ENV = "ALFRED_TELEGRAM_BOT_TOKEN"
DATABASE_URL_ENV = "ALFRED_DATABASE_URL"

RESERVED_SERVER_NAME = "alfred"
_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
_TOKEN_ENV = re.compile(r"ALFRED_[A-Z0-9_]+")


class ConfigError(Exception):
    """Invalid configuration. ``problems`` are safe to print and log."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("invalid configuration:\n" + "\n".join(f"  - {p}" for p in problems))


# --- field types ---------------------------------------------------------------


def _check_name(value: str) -> str:
    if not _NAME.fullmatch(value):
        raise ValueError("must be lowercase letters, digits and hyphens")
    return value


def _check_http_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("must be an http:// or https:// URL with a host")
    return value.rstrip("/")


def _check_token_env(value: str) -> str:
    if not _TOKEN_ENV.fullmatch(value):
        raise ValueError("must name an ALFRED_* environment variable (A-Z, 0-9, _)")
    return value


Name = Annotated[str, AfterValidator(_check_name)]
HttpUrlStr = Annotated[str, AfterValidator(_check_http_url)]
TokenEnv = Annotated[str, AfterValidator(_check_token_env)]
PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- file sections -------------------------------------------------------------


class TelegramConfig(_Section):
    owner_user_id: PositiveInt


class AIConfig(_Section):
    base_url: HttpUrlStr = "http://ai.lan"
    model: Annotated[str, Field(min_length=1)] = "gemma4:12b-it-qat"
    request_timeout_s: PositiveFloat = 300
    max_requests_per_turn: PositiveInt = 8
    tool_result_max_chars: PositiveInt = 4000
    system_prompt: Annotated[str, Field(min_length=1)] | None = None


class HistoryConfig(_Section):
    max_entries: PositiveInt = 30


class QueueConfig(_Section):
    retry_initial_s: PositiveFloat = 30
    retry_max_s: PositiveFloat = 900

    @model_validator(mode="after")
    def _max_not_below_initial(self) -> QueueConfig:
        if self.retry_max_s < self.retry_initial_s:
            raise ValueError("retry_max_s must not be smaller than retry_initial_s")
        return self


class MCPConfig(_Section):
    list_timeout_s: PositiveFloat = 10
    call_timeout_s: PositiveFloat = 60


class ServerConfig(_Section):
    host: Annotated[str, Field(min_length=1)] = "0.0.0.0"
    port: Annotated[int, Field(ge=1, le=65535)] = 8080
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class ServiceEntry(_Section):
    name: Name
    token_env: TokenEnv


class McpServerEntry(_Section):
    name: Name
    url: HttpUrlStr
    token_env: TokenEnv
    exclude_tools: list[Annotated[str, Field(min_length=1)]] = []


class FileConfig(_Section):
    """The shape of ``alfred.toml``."""

    telegram: TelegramConfig
    ai: AIConfig = AIConfig()
    history: HistoryConfig = HistoryConfig()
    queue: QueueConfig = QueueConfig()
    mcp: MCPConfig = MCPConfig()
    server: ServerConfig = ServerConfig()
    services: list[ServiceEntry] = []
    mcp_servers: list[McpServerEntry] = []


# --- resolved configuration ----------------------------------------------------


@dataclass(frozen=True)
class Service:
    name: str
    token: SecretStr


@dataclass(frozen=True)
class McpServer:
    name: str
    url: str
    token: SecretStr
    exclude_tools: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Config:
    """The validated configuration with its secrets resolved."""

    telegram: TelegramConfig
    ai: AIConfig
    history: HistoryConfig
    queue: QueueConfig
    mcp: MCPConfig
    server: ServerConfig
    services: tuple[Service, ...]
    mcp_servers: tuple[McpServer, ...]
    telegram_bot_token: SecretStr
    database_url: SecretStr


# --- loading -------------------------------------------------------------------


def config_path(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    return Path(source.get(CONFIG_ENV) or DEFAULT_CONFIG_PATH)


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Load ``alfred.toml`` and the ``ALFRED_*`` secrets from ``env``.

    Raises :class:`ConfigError` listing every problem found.
    """
    source = os.environ if env is None else env
    path = config_path(source)
    data = _read_toml(path)

    problems: list[str] = []
    parsed: FileConfig | None = None
    try:
        parsed = FileConfig.model_validate(data)
    except ValidationError as exc:
        problems += [_describe(e, data) for e in exc.errors(include_input=False, include_url=False)]

    services_raw = _entries(data, "services")
    servers_raw = _entries(data, "mcp_servers")
    problems += _duplicate_names("services", services_raw)
    problems += _duplicate_names("mcp_servers", servers_raw)
    for entry in servers_raw:
        if entry.get("name") == RESERVED_SERVER_NAME:
            problems.append(
                f"mcp_servers: the name {RESERVED_SERVER_NAME!r} is reserved for alfred's own tools"
            )

    bot_token = _require_env(source, BOT_TOKEN_ENV, problems)
    database_url = load_database_url(source, problems)
    service_tokens = _resolve_tokens(source, "services", services_raw, problems)
    server_tokens = _resolve_tokens(source, "mcp_servers", servers_raw, problems)
    problems += _duplicate_tokens(service_tokens)

    if problems or parsed is None:
        raise ConfigError(problems)

    return Config(
        telegram=parsed.telegram,
        ai=parsed.ai,
        history=parsed.history,
        queue=parsed.queue,
        mcp=parsed.mcp,
        server=parsed.server,
        services=tuple(Service(s.name, SecretStr(service_tokens[s.name])) for s in parsed.services),
        mcp_servers=tuple(
            McpServer(m.name, m.url, SecretStr(server_tokens[m.name]), frozenset(m.exclude_tools))
            for m in parsed.mcp_servers
        ),
        telegram_bot_token=SecretStr(bot_token or ""),
        database_url=SecretStr(database_url or ""),
    )


def load_database_url(
    env: Mapping[str, str] | None = None, problems: list[str] | None = None
) -> str | None:
    """Read ``ALFRED_DATABASE_URL`` and point SQLAlchemy at psycopg 3.

    With ``problems`` given, errors are appended there and ``None`` returned;
    otherwise a :class:`ConfigError` is raised. Messages never include the URL.
    """
    source = os.environ if env is None else env
    own: list[str] = []
    collect = own if problems is None else problems
    value = _require_env(source, DATABASE_URL_ENV, collect)
    result: str | None = None
    if value is not None:
        for prefix in ("postgresql://", "postgres://"):
            if value.startswith(prefix):
                result = "postgresql+psycopg://" + value[len(prefix) :]
                break
        else:
            if value.startswith("postgresql+psycopg://"):
                result = value
            else:
                collect.append(f"{DATABASE_URL_ENV} must be a postgresql:// URL")
    if problems is None and own:
        raise ConfigError(own)
    return result


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError([f"config file {str(path)!r} not found (set {CONFIG_ENV})"]) from None
    except OSError as exc:
        raise ConfigError([f"config file {str(path)!r} can't be read: {exc.strerror}"]) from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"config file {str(path)!r} is not valid TOML: {exc}"]) from None


def _entries(data: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key)
    if not isinstance(value, list):
        return []
    return [e for e in value if isinstance(e, dict)]


def _label(section: str, entry: Mapping[str, Any], index: int) -> str:
    name = entry.get("name")
    return f"{section}[{index}] ({name})" if isinstance(name, str) else f"{section}[{index}]"


def _duplicate_names(section: str, entries: list[dict[str, Any]]) -> list[str]:
    counts = Counter(e["name"] for e in entries if isinstance(e.get("name"), str))
    return [f"{section}: duplicate name {name!r}" for name, n in counts.items() if n > 1]


def _require_env(source: Mapping[str, str], var: str, problems: list[str]) -> str | None:
    value = source.get(var, "").strip()
    if not value:
        problems.append(f"{var} is required but not set")
        return None
    return value


def _resolve_tokens(
    source: Mapping[str, str], section: str, entries: list[dict[str, Any]], problems: list[str]
) -> dict[str, str]:
    """Map entry name -> token for entries whose ``token_env`` is set."""
    tokens: dict[str, str] = {}
    for i, entry in enumerate(entries):
        var = entry.get("token_env")
        if not isinstance(var, str) or not _TOKEN_ENV.fullmatch(var):
            continue  # reported by the model validation
        value = source.get(var, "").strip()
        if not value:
            problems.append(f"{var} is not set or empty (token_env of {_label(section, entry, i)})")
            continue
        name = entry.get("name")
        if isinstance(name, str):
            tokens[name] = value
    return tokens


def _duplicate_tokens(tokens: Mapping[str, str]) -> list[str]:
    by_token: dict[str, list[str]] = {}
    for name, token in tokens.items():
        by_token.setdefault(token, []).append(name)
    return [
        "services " + ", ".join(repr(n) for n in sorted(names)) + " share the same token"
        for names in by_token.values()
        if len(names) > 1
    ]


def _describe(error: Mapping[str, Any], data: Mapping[str, Any]) -> str:
    """One pydantic error as ``where: what``, without the input value."""
    loc = tuple(error.get("loc") or ())
    parts: list[str] = []
    node: Any = data
    for item in loc:
        if isinstance(item, int):
            name = node[item].get("name") if isinstance(node, list) and item < len(node) else None
            entry = node[item] if isinstance(node, list) and item < len(node) else None
            parts[-1] += f"[{item}]" + (f" ({name})" if isinstance(name, str) else "")
            node = entry
        else:
            parts.append(str(item))
            node = node.get(item) if isinstance(node, Mapping) else None
    where = ".".join(parts) or "config file"
    kind = error.get("type")
    if kind == "extra_forbidden":
        return f"{where}: unknown key"
    if kind == "missing":
        return f"{where}: required but not set"
    msg = str(error.get("msg", "invalid value")).removeprefix("Value error, ")
    return f"{where}: {msg}"
