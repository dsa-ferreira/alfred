"""Startup configuration: ``alfred.toml`` plus ``ALFRED_*`` secret variables.

Every non-secret setting lives in the TOML file (path from ``ALFRED_CONFIG``,
default ``alfred.toml``). Secrets are only read from environment variables:
``ALFRED_TELEGRAM_BOT_TOKEN``, ``ALFRED_DATABASE_URL``, the object store key
(``ALFRED_S3_ACCESS_KEY_ID`` / ``ALFRED_S3_SECRET_ACCESS_KEY``, only with
``[attachments]``), the variables named by each service's and MCP server's
``token_env``, and those named in users' ``mcp_tokens``.

Everything is validated once. All problems are collected into one
:class:`ConfigError` whose messages name keys and variables but never include
a secret value.
"""

from __future__ import annotations

import dataclasses
import os
import re
import tomllib
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from alfred.tools.base import AllowList

CONFIG_ENV = "ALFRED_CONFIG"
DEFAULT_CONFIG_PATH = "alfred.toml"
BOT_TOKEN_ENV = "ALFRED_TELEGRAM_BOT_TOKEN"
DATABASE_URL_ENV = "ALFRED_DATABASE_URL"
S3_ACCESS_KEY_ID_ENV = "ALFRED_S3_ACCESS_KEY_ID"
S3_SECRET_ACCESS_KEY_ENV = "ALFRED_S3_SECRET_ACCESS_KEY"
# Telegram bots can't download files larger than this.
TELEGRAM_MAX_BYTES = 20_000_000

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


def _check_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("unknown timezone; use an IANA name such as Europe/Lisbon") from None
    return value


def _check_token_env(value: str) -> str:
    if not _TOKEN_ENV.fullmatch(value):
        raise ValueError("must name an ALFRED_* environment variable (A-Z, 0-9, _)")
    return value


Name = Annotated[str, AfterValidator(_check_name)]
HttpUrlStr = Annotated[str, AfterValidator(_check_http_url)]
TokenEnv = Annotated[str, AfterValidator(_check_token_env)]
TimeZoneName = Annotated[str, AfterValidator(_check_timezone)]
PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- file sections -------------------------------------------------------------


class TelegramConfig(_Section):
    """No settings of its own: who may chat is set by ``[[users]]``.

    Kept so the removed ``owner_user_id`` is reported as an unknown key.
    """


class BrainEntry(_Section):
    """An ``[ai.models]`` entry written as a table (change add-attachments, design D9)."""

    model: Annotated[str, Field(min_length=1)]
    vision: bool = False


def _brain_entries(value: Any) -> Any:
    """``alias = "tag"`` is short for ``alias = { model = "tag" }``."""
    if not isinstance(value, Mapping):
        return value
    return {k: {"model": v} if isinstance(v, str) else v for k, v in value.items()}


class AIConfig(_Section):
    base_url: HttpUrlStr = "http://ai.lan"
    model: Annotated[str, Field(min_length=1)] = "gemma4:26b-a4b-it-qat"
    # Whether the [ai] model can see images, when no alias names it.
    vision: bool = False
    request_timeout_s: PositiveFloat = 300
    max_requests_per_turn: PositiveInt = 12
    tool_result_max_chars: PositiveInt = 4000
    persona: Annotated[str, Field(min_length=1)] | None = None
    # alfred's timezone: today's date in the prompt, reminders and search dates.
    timezone: TimeZoneName = "Europe/Lisbon"
    # The brains the admin can switch between: alias -> model tag, or
    # alias -> { model = tag, vision = true }. Both are read as a BrainEntry.
    models: Annotated[dict[Name, BrainEntry], BeforeValidator(_brain_entries)] = {}

    @field_validator("models")
    @classmethod
    def _check_models(
        cls, models: dict[str, BrainEntry], info: ValidationInfo
    ) -> dict[str, BrainEntry]:
        by_tag: dict[str, list[str]] = {}
        for alias, entry in models.items():
            by_tag.setdefault(entry.model, []).append(alias)
        problems = [
            "aliases " + ", ".join(repr(a) for a in aliases) + f" all name {tag!r}"
            for tag, aliases in by_tag.items()
            if len(aliases) > 1
        ]
        # The startup model is a brain under its own tag when no alias names it.
        model = info.data.get("model")
        if isinstance(model, str) and model not in by_tag and model in models:
            problems.append(f"alias {model!r} is also the name of the [ai] model's brain")
        if problems:
            raise ValueError("; ".join(problems))
        return models


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


class MemoryConfig(_Section):
    """Summaries, search and recall of archived conversations (memory design D11)."""

    # None: the ``[ai]`` model (see :attr:`Config.summary_model`).
    summary_model: Annotated[str, Field(min_length=1)] | None = None
    embedding_base_url: HttpUrlStr = "http://ragman.lan"
    embedding_model: Annotated[str, Field(min_length=1)] = "nomic-embed-text:v1.5"
    embedding_timeout_s: PositiveFloat = 30
    document_prefix: str = "search_document: "
    query_prefix: str = "search_query: "
    recall_max_chars: PositiveInt = 32000
    summary_input_max_chars: PositiveInt = 60000
    search_results: PositiveInt = 5


class WebConfig(_Section):
    """Web search through SearXNG and page fetch (change add-web-search, design D9)."""

    searxng_url: HttpUrlStr = "http://oracle.lan"
    search_results: PositiveInt = 5
    fetch_page_chars: PositiveInt = 8000
    fetch_timeout_s: PositiveFloat = 20
    fetch_max_bytes: PositiveInt = 5_000_000


_CLOCK = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")


def _check_clock(value: str) -> str:
    if not _CLOCK.fullmatch(value):
        raise ValueError("must be a 24-hour time HH:MM, such as 09:00")
    return value


class RemindersConfig(_Section):
    """Timed reminders (change add-reminders, design D9)."""

    # The time of a reminder on a date with no clock time ("on Friday").
    default_time: Annotated[str, AfterValidator(_check_clock)] = "09:00"
    # Checks run on the clock: at every whole minute and every
    # check_interval_s within it, so at most 60.
    check_interval_s: Annotated[float, Field(gt=0, le=60)] = 30


class AttachmentsConfig(_Section):
    """Images and PDFs from users (change add-attachments, design D1 and D10).

    The section's presence turns attachments on; the object store key comes
    from ``ALFRED_S3_ACCESS_KEY_ID`` / ``ALFRED_S3_SECRET_ACCESS_KEY``.
    """

    endpoint_url: HttpUrlStr
    bucket: Annotated[str, Field(min_length=1)]
    region: Annotated[str, Field(min_length=1)] = "garage"
    max_bytes: Annotated[int, Field(gt=0)] = TELEGRAM_MAX_BYTES
    # How many of the newest user messages carry their attachments inline.
    inline_turns: Annotated[int, Field(ge=0)] = 1
    first_page_chars: PositiveInt = 4000
    read_chars: PositiveInt = 8000
    image_max_px: PositiveInt = 1536
    album_wait_s: PositiveFloat = 2

    @field_validator("max_bytes")
    @classmethod
    def _within_telegram_limit(cls, value: int) -> int:
        if value > TELEGRAM_MAX_BYTES:
            raise ValueError(
                f"must be at most {TELEGRAM_MAX_BYTES} (20 MB, the largest file a bot can download)"
            )
        return value


class MCPConfig(_Section):
    list_timeout_s: PositiveFloat = 10
    call_timeout_s: PositiveFloat = 60
    instructions_max_chars: PositiveInt = 2000


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
    use_server_instructions: bool = True


ALL_TOOLS = "*"


class UserEntry(_Section):
    """A ``[[users]]`` entry (change add-multiple-users, design D1)."""

    name: Name
    telegram_user_id: PositiveInt
    # How alfred addresses this user, in replies and fixed notices.
    address: Annotated[str, Field(min_length=1)] = "sir"
    admin: bool = False
    # Model-facing tool names, ``prefix*`` patterns, or ``*``.
    tools: list[Annotated[str, Field(min_length=1)]] = []
    # MCP server name -> the variable holding this user's token for it.
    mcp_tokens: dict[Name, TokenEnv] = {}


class FileConfig(_Section):
    """The shape of ``alfred.toml``."""

    users: Annotated[list[UserEntry], Field(min_length=1)]
    telegram: TelegramConfig = TelegramConfig()
    ai: AIConfig = AIConfig()
    history: HistoryConfig = HistoryConfig()
    queue: QueueConfig = QueueConfig()
    memory: MemoryConfig = MemoryConfig()
    web: WebConfig = WebConfig()
    reminders: RemindersConfig = RemindersConfig()
    # None: attachments are off and alfred reads text only.
    attachments: AttachmentsConfig | None = None
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
    use_server_instructions: bool = True


@dataclass(frozen=True)
class User:
    """Someone alfred serves (change add-multiple-users, design D1)."""

    name: str
    telegram_user_id: int
    address: str = "sir"
    admin: bool = False
    tools: AllowList = AllowList()
    # MCP server name -> this user's token, where it isn't the server's own.
    mcp_tokens: Mapping[str, SecretStr] = field(default_factory=dict)


@dataclass(frozen=True)
class Users:
    """The configured users, in file order. Exactly one is the admin."""

    all: tuple[User, ...]

    def __iter__(self) -> Iterator[User]:
        return iter(self.all)

    def __len__(self) -> int:
        return len(self.all)

    @property
    def names(self) -> list[str]:
        return [u.name for u in self.all]

    @property
    def admin(self) -> User:
        return next(u for u in self.all if u.admin)

    def get(self, name: str) -> User | None:
        return next((u for u in self.all if u.name == name), None)

    def by_telegram_id(self, telegram_user_id: int) -> User | None:
        return next((u for u in self.all if u.telegram_user_id == telegram_user_id), None)


@dataclass(frozen=True)
class S3Key:
    access_key_id: SecretStr
    secret_access_key: SecretStr


@dataclass(frozen=True)
class Config:
    """The validated configuration with its secrets resolved."""

    users: Users
    ai: AIConfig
    history: HistoryConfig
    queue: QueueConfig
    mcp: MCPConfig
    server: ServerConfig
    services: tuple[Service, ...]
    mcp_servers: tuple[McpServer, ...]
    telegram_bot_token: SecretStr
    database_url: SecretStr
    memory: MemoryConfig = MemoryConfig()
    web: WebConfig = WebConfig()
    reminders: RemindersConfig = RemindersConfig()
    attachments: AttachmentsConfig | None = None
    # Set exactly when ``attachments`` is.
    s3_key: S3Key | None = None

    @property
    def summary_model(self) -> str:
        """The model that summarises ended conversations: ``[memory]``'s, else ``[ai]``'s."""
        return self.memory.summary_model or self.ai.model

    def servers_for(self, user: User) -> tuple[McpServer, ...]:
        """The MCP servers with the token to use on ``user``'s turns (design D8)."""
        return tuple(
            dataclasses.replace(s, token=user.mcp_tokens[s.name])
            if s.name in user.mcp_tokens
            else s
            for s in self.mcp_servers
        )


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
    users_raw = _entries(data, "users")
    problems += _duplicate_names("services", services_raw)
    problems += _duplicate_names("mcp_servers", servers_raw)
    server_names = {e["name"] for e in servers_raw if isinstance(e.get("name"), str)}
    problems += _check_users(users_raw, server_names)
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
    # Users may share a token with each other or with the server (design D8).
    user_tokens = _resolve_user_tokens(source, users_raw, problems)
    s3_key: S3Key | None = None
    if isinstance(data.get("attachments"), Mapping):
        key_id = _require_env(source, S3_ACCESS_KEY_ID_ENV, problems, "[attachments] is set")
        secret = _require_env(source, S3_SECRET_ACCESS_KEY_ENV, problems, "[attachments] is set")
        if key_id is not None and secret is not None:
            s3_key = S3Key(SecretStr(key_id), SecretStr(secret))

    if problems or parsed is None:
        raise ConfigError(problems)

    return Config(
        users=Users(
            tuple(
                User(
                    u.name,
                    u.telegram_user_id,
                    u.address,
                    u.admin,
                    AllowList(tuple(u.tools)),
                    {s: SecretStr(user_tokens[(u.name, s)]) for s in u.mcp_tokens},
                )
                for u in parsed.users
            )
        ),
        ai=parsed.ai,
        history=parsed.history,
        queue=parsed.queue,
        memory=parsed.memory,
        web=parsed.web,
        reminders=parsed.reminders,
        attachments=parsed.attachments,
        s3_key=s3_key if parsed.attachments is not None else None,
        mcp=parsed.mcp,
        server=parsed.server,
        services=tuple(Service(s.name, SecretStr(service_tokens[s.name])) for s in parsed.services),
        mcp_servers=tuple(
            McpServer(
                m.name,
                m.url,
                SecretStr(server_tokens[m.name]),
                frozenset(m.exclude_tools),
                m.use_server_instructions,
            )
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


def _require_env(
    source: Mapping[str, str], var: str, problems: list[str], because: str | None = None
) -> str | None:
    value = source.get(var, "").strip()
    if not value:
        why = f" ({because})" if because else ""
        problems.append(f"{var} is required but not set{why}")
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


def _check_users(users: list[dict[str, Any]], server_names: set[str]) -> list[str]:
    """What the field types can't check: uniqueness, the one admin, server names, tools."""
    problems = _duplicate_names("users", users)
    by_id: dict[int, list[str]] = {}
    for i, user in enumerate(users):
        user_id = user.get("telegram_user_id")
        if isinstance(user_id, int):
            by_id.setdefault(user_id, []).append(_who(user, i))
    problems += [
        "users " + ", ".join(names) + " have the same telegram_user_id"
        for names in by_id.values()
        if len(names) > 1
    ]
    admins = [_who(u, i) for i, u in enumerate(users) if u.get("admin") is True]
    if users and not admins:
        problems.append("users: exactly one admin is required; set admin = true on one user")
    elif len(admins) > 1:
        problems.append(
            "users " + ", ".join(admins) + " are all admins; exactly one admin is required"
        )
    known_tools: frozenset[str] | None = None
    for i, user in enumerate(users):
        label = _label("users", user, i)
        tokens = user.get("mcp_tokens")
        if isinstance(tokens, Mapping):
            for server in tokens:
                if server not in server_names:
                    problems.append(
                        f"{label}: mcp_tokens names {server!r}, "
                        "which is not a configured MCP server"
                    )
        tools = user.get("tools")
        for entry in tools if isinstance(tools, list) else ():
            if not isinstance(entry, str) or not entry or entry == ALL_TOOLS:
                continue  # wrong types are reported by the model validation
            if entry.startswith("alfred_"):
                if entry.endswith("*"):
                    continue
                if known_tools is None:
                    from alfred.tools.catalog import alfred_tool_names

                    known_tools = alfred_tool_names()
                if entry not in known_tools:
                    problems.append(f"{label}: tools entry {entry!r} is not an alfred tool")
            elif not any(entry.startswith(f"{server}_") for server in server_names):
                problems.append(
                    f"{label}: tools entry {entry!r} must be *, or start with alfred_ or "
                    "with a configured MCP server's name and _"
                )
    return problems


def _who(entry: Mapping[str, Any], index: int) -> str:
    name = entry.get("name")
    return repr(name) if isinstance(name, str) else f"users[{index}]"


def _resolve_user_tokens(
    source: Mapping[str, str], users: list[dict[str, Any]], problems: list[str]
) -> dict[tuple[str, str], str]:
    """Map (user name, server name) -> token for every ``mcp_tokens`` variable that is set."""
    tokens: dict[tuple[str, str], str] = {}
    for i, user in enumerate(users):
        refs = user.get("mcp_tokens")
        name = user.get("name")
        if not isinstance(refs, Mapping):
            continue
        for server, var in refs.items():
            if not isinstance(var, str) or not _TOKEN_ENV.fullmatch(var):
                continue  # reported by the model validation
            value = source.get(var, "").strip()
            if not value:
                problems.append(
                    f"{var} is not set or empty (mcp_tokens.{server} of {_label('users', user, i)})"
                )
            elif isinstance(name, str):
                tokens[(name, server)] = value
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
        if item == "[key]":  # a dict key that failed validation; the key is already named
            continue
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
