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
import logging
import os
import re
import tomllib
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from alfred.kernel.access import AllowList
from alfred.kernel.users import User, Users
from alfred.settings import (
    TOKEN_ENV,
    ConfigError,
    HttpUrlStr,
    Name,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    Section,
    TimeZoneName,
    TokenEnv,
)

log = logging.getLogger(__name__)

_CONFIG_ENV = "ALFRED_CONFIG"
_DEFAULT_CONFIG_PATH = "alfred.toml"
_BOT_TOKEN_ENV = "ALFRED_TELEGRAM_BOT_TOKEN"
_DATABASE_URL_ENV = "ALFRED_DATABASE_URL"
_S3_ACCESS_KEY_ID_ENV = "ALFRED_S3_ACCESS_KEY_ID"
_S3_SECRET_ACCESS_KEY_ENV = "ALFRED_S3_SECRET_ACCESS_KEY"
# Telegram bots can't download files larger than this.
_TELEGRAM_MAX_BYTES = 20_000_000

_RESERVED_SERVER_NAME = "alfred"
_CLOCK = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")
_ALL_TOOLS = "*"


class BrainEntry(Section):
    """An ``[ai.models]`` entry written as a table."""

    model: Annotated[str, Field(min_length=1)]
    vision: bool = False


def _brain_entries(value: Any) -> Any:
    """``alias = "tag"`` is short for ``alias = { model = "tag" }``."""
    if not isinstance(value, Mapping):
        return value
    return {k: {"model": v} if isinstance(v, str) else v for k, v in value.items()}


class AIConfig(Section):
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


class QueueConfig(Section):
    retry_initial_s: PositiveFloat = 30
    retry_max_s: PositiveFloat = 900

    @model_validator(mode="after")
    def _max_not_below_initial(self) -> QueueConfig:
        if self.retry_max_s < self.retry_initial_s:
            raise ValueError("retry_max_s must not be smaller than retry_initial_s")
        return self


class MemoryConfig(Section):
    """Summaries, search and recall of archived conversations."""

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
    # Seconds the queue must have been quiet before a summary request
    # starts. 0: only an empty queue.
    summary_quiet_s: NonNegativeFloat = 60
    # User facts. Off: facts are learned, remembered and listed by
    # /memory, but sent in no prompt.
    facts_in_context: bool = False
    # Most characters of facts in one system prompt.
    facts_context_max_chars: PositiveInt = 3000
    # Longest fact; longer learned facts are cut, longer remembered ones refused.
    fact_max_chars: PositiveInt = 300
    # Failed extractions of one conversation before giving up. 0: never.
    facts_extraction_max_attempts: NonNegativeInt = 0
    # Cosine distance within which a user's facts are checked for duplicates by
    # the model. Distance alone decides nothing.
    facts_duplicate_max_distance: Annotated[float, Field(gt=0, lt=1)] = 0.2


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
class S3Key:
    access_key_id: SecretStr
    secret_access_key: SecretStr


class _HistoryConfig(Section):
    max_entries: PositiveInt = 30
    # Hours after a user's last own message before their conversation is
    # closed. None: never.
    idle_close_after_h: PositiveFloat | None = None


class _WebConfig(Section):
    """Web search through SearXNG and page fetch."""

    searxng_url: HttpUrlStr = "http://oracle.lan"
    search_results: PositiveInt = 5
    fetch_page_chars: PositiveInt = 8000
    fetch_timeout_s: PositiveFloat = 20
    fetch_max_bytes: PositiveInt = 5_000_000


def _check_clock(value: str) -> str:
    if not _CLOCK.fullmatch(value):
        raise ValueError("must be a 24-hour time HH:MM, such as 09:00")
    return value


class _RemindersConfig(Section):
    """Timed reminders."""

    # The time of a reminder on a date with no clock time ("on Friday").
    default_time: Annotated[str, AfterValidator(_check_clock)] = "09:00"
    # Checks run on the clock: at every whole minute and every
    # check_interval_s within it, so at most 60.
    check_interval_s: Annotated[float, Field(gt=0, le=60)] = 30


class _InputRequestsConfig(Section):
    """Turns waiting for their user's answer."""

    # Counted from when the question was sent.
    expire_after_min: PositiveFloat = 60


class _MCPConfig(Section):
    list_timeout_s: PositiveFloat = 10
    call_timeout_s: PositiveFloat = 60
    instructions_max_chars: PositiveInt = 2000


class _ServerConfig(Section):
    host: Annotated[str, Field(min_length=1)] = "0.0.0.0"
    port: Annotated[int, Field(ge=1, le=65535)] = 8080
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


@dataclass(frozen=True)
class Config:
    """The validated configuration with its secrets resolved."""

    users: Users
    ai: AIConfig
    history: _HistoryConfig
    queue: QueueConfig
    mcp: _MCPConfig
    server: _ServerConfig
    services: tuple[Service, ...]
    mcp_servers: tuple[McpServer, ...]
    telegram_bot_token: SecretStr
    database_url: SecretStr
    memory: MemoryConfig = MemoryConfig()
    web: _WebConfig = _WebConfig()
    reminders: _RemindersConfig = _RemindersConfig()
    input_requests: _InputRequestsConfig = _InputRequestsConfig()
    attachments: AttachmentsConfig | None = None
    # Set exactly when ``attachments`` is.
    s3_key: S3Key | None = None

    @property
    def summary_model(self) -> str:
        """The model that summarises ended conversations: ``[memory]``'s, else ``[ai]``'s."""
        return self.memory.summary_model or self.ai.model

    def servers_for(self, user: User) -> tuple[McpServer, ...]:
        """The MCP servers with the token to use on ``user``'s turns."""
        return tuple(
            dataclasses.replace(s, token=user.mcp_tokens[s.name])
            if s.name in user.mcp_tokens
            else s
            for s in self.mcp_servers
        )


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Load ``alfred.toml`` and the ``ALFRED_*`` secrets from ``env``.

    Raises :class:`ConfigError` listing every problem found.
    """
    source = os.environ if env is None else env
    path = _config_path(source)
    data = _read_toml(path)

    problems: list[str] = []
    parsed: _FileConfig | None = None
    try:
        parsed = _FileConfig.model_validate(data)
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
        if entry.get("name") == _RESERVED_SERVER_NAME:
            problems.append(
                f"mcp_servers: the name {_RESERVED_SERVER_NAME!r} "
                "is reserved for alfred's own tools"
            )

    bot_token = _require_env(source, _BOT_TOKEN_ENV, problems)
    database_url = load_database_url(source, problems)
    service_tokens = _resolve_tokens(source, "services", services_raw, problems)
    server_tokens = _resolve_tokens(source, "mcp_servers", servers_raw, problems)
    problems += _duplicate_tokens(service_tokens)
    # Users may share a token with each other or with the server.
    user_tokens = _resolve_user_tokens(source, users_raw, problems)
    s3_key: S3Key | None = None
    if isinstance(data.get("attachments"), Mapping):
        key_id = _require_env(source, _S3_ACCESS_KEY_ID_ENV, problems, "[attachments] is set")
        secret = _require_env(source, _S3_SECRET_ACCESS_KEY_ENV, problems, "[attachments] is set")
        if key_id is not None and secret is not None:
            s3_key = S3Key(SecretStr(key_id), SecretStr(secret))

    if problems or parsed is None:
        raise ConfigError(problems)
    _warn_hidden_confirms(parsed.users)

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
                    AllowList(tuple(u.confirm)),
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
        input_requests=parsed.input_requests,
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
    value = _require_env(source, _DATABASE_URL_ENV, collect)
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
                collect.append(f"{_DATABASE_URL_ENV} must be a postgresql:// URL")
    if problems is None and own:
        raise ConfigError(own)
    return result


class _TelegramConfig(Section):
    """No settings of its own: who may chat is set by ``[[users]]``.

    Kept so the removed ``owner_user_id`` is reported as an unknown key.
    """


class _ServiceEntry(Section):
    name: Name
    token_env: TokenEnv


class _McpServerEntry(Section):
    name: Name
    url: HttpUrlStr
    token_env: TokenEnv
    exclude_tools: list[Annotated[str, Field(min_length=1)]] = []
    use_server_instructions: bool = True


class _UserEntry(Section):
    """A ``[[users]]`` entry."""

    name: Name
    telegram_user_id: PositiveInt
    # How alfred addresses this user, in replies and fixed notices.
    address: Annotated[str, Field(min_length=1)] = "sir"
    admin: bool = False
    # Model-facing tool names, ``prefix*`` patterns, or ``*``.
    tools: list[Annotated[str, Field(min_length=1)]] = []
    # Tools whose calls on this user's turns wait for their confirmation, in
    # the same syntax.
    confirm: list[Annotated[str, Field(min_length=1)]] = []
    # MCP server name -> the variable holding this user's token for it.
    mcp_tokens: dict[Name, TokenEnv] = {}


class _FileConfig(Section):
    """The shape of ``alfred.toml``."""

    users: Annotated[list[_UserEntry], Field(min_length=1)]
    telegram: _TelegramConfig = _TelegramConfig()
    ai: AIConfig = AIConfig()
    history: _HistoryConfig = _HistoryConfig()
    queue: QueueConfig = QueueConfig()
    memory: MemoryConfig = MemoryConfig()
    web: _WebConfig = _WebConfig()
    reminders: _RemindersConfig = _RemindersConfig()
    input_requests: _InputRequestsConfig = _InputRequestsConfig()
    # None: attachments are off and alfred reads text only.
    attachments: AttachmentsConfig | None = None
    mcp: _MCPConfig = _MCPConfig()
    server: _ServerConfig = _ServerConfig()
    services: list[_ServiceEntry] = []
    mcp_servers: list[_McpServerEntry] = []


def _config_path(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    return Path(source.get(_CONFIG_ENV) or _DEFAULT_CONFIG_PATH)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError([f"config file {str(path)!r} not found (set {_CONFIG_ENV})"]) from None
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
        if not isinstance(var, str) or not TOKEN_ENV.fullmatch(var):
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
        # The confirm list has the allow-list's syntax and rules.
        for key in ("tools", "confirm"):
            entries = user.get(key)
            for entry in entries if isinstance(entries, list) else ():
                problem = _tool_entry_problem(entry, server_names)
                if problem is not None:
                    problems.append(f"{label}: {key} entry {entry!r} {problem}")
    return problems


def _tool_entry_problem(entry: object, server_names: set[str]) -> str | None:
    """Why an allow-list or confirm-list entry is wrong, or ``None``."""
    if not isinstance(entry, str) or not entry or entry == _ALL_TOOLS:
        return None  # wrong types are reported by the model validation
    if entry.startswith("alfred_"):
        if entry.endswith("*"):
            return None
        from alfred.tools.catalog import alfred_tool_names

        return None if entry in alfred_tool_names() else "is not an alfred tool"
    if any(entry.startswith(f"{server}_") for server in server_names):
        return None
    return "must be *, or start with alfred_ or with a configured MCP server's name and _"


def _warn_hidden_confirms(users: list[_UserEntry]) -> None:
    """Log confirm entries the user's allow-list can never match.

    Not an error: the entry is harmless, only useless until the allow-list
    changes.
    """
    for user in users:
        tools = AllowList(tuple(user.tools))
        for entry in user.confirm:
            reachable = (
                tools.may_match_prefix(entry[:-1]) if entry.endswith("*") else tools.matches(entry)
            )
            if not reachable:
                log.warning(
                    "user %s: confirm entry %r matches no tool their tools list offers",
                    user.name,
                    entry,
                )


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
            if not isinstance(var, str) or not TOKEN_ENV.fullmatch(var):
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
