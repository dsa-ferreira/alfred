"""The tools offered in one turn, and routing the model's calls to them.

At the start of every turn each configured server's tools are listed
concurrently. Servers that fail are reported as unavailable for that turn.
Tools are offered as ``<server>_<tool>``; the routing map built here is the
only way a call reaches a server, so prefixes are never parsed. The same
connection also yields the instructions each server sends, which the turn
gives the model.

A server may answer a call by asking the user something
(``InputRequiredResult``). ``classify`` turns a question that fits buttons
into a need for the user's answer, carrying the server's opaque
``request_state``; anything else is an error result, and nothing is sent
back. ``resume_tool`` repeats the call on a fresh connection with the
answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.types import (
    HEADER_MISMATCH,
    INVALID_PARAMS,
    CallToolResult,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
    TextContent,
    Tool,
)

from alfred.config import McpServer
from alfred.kernel.questions import InputNeed, Option, Question
from alfred.kernel.toolset import ToolOutcome, parse_arguments
from alfred.mcp.client import Connector

log = logging.getLogger(__name__)

_INVALID_CHARS = re.compile(r"[^a-zA-Z0-9_-]")
_MAX_TOOL_NAME = 64
# Questions a call may ask before alfred gives up (the SDK's default).
_MAX_INPUT_ROUNDS = 10
# Choices that fit as buttons.
_MAX_CHOICES = 8
# The resume state of a server's question.
_VIA_MCP = "mcp"
# Waits before resending a leg the server answered with request_state only
# (the SDK's backoff: 50 ms doubling to 250 ms).
_STATE_ONLY_DELAYS_S = (0.05, 0.1, 0.2) + (0.25,) * (_MAX_INPUT_ROUNDS - 3)


_YES, _NO, _CANCEL = "Yes", "No", "Cancel"
_DECLINE = {"action": "decline"}


@dataclass(frozen=True)
class ServerInstructions:
    """Instructions a server sent when connecting, ready for the model."""

    server: str
    text: str


@dataclass(frozen=True)
class _Route:
    server: McpServer
    tool: str


@dataclass
class TurnTools:
    """What one turn may call."""

    definitions: list[dict[str, Any]] = field(default_factory=list)
    routes: dict[str, _Route] = field(default_factory=dict)
    # Model-facing names of configured exclusions, refused without a network call.
    excluded: set[str] = field(default_factory=set)
    unavailable: list[str] = field(default_factory=list)
    # Available, unmuted servers with non-blank instructions, in configuration order.
    instructions: list[ServerInstructions] = field(default_factory=list)


async def build_turn_tools(
    servers: Sequence[McpServer],
    connect: Connector,
    list_timeout_s: float,
    *,
    instructions_max_chars: int = 2000,
) -> TurnTools:
    """List every server's tools and instructions concurrently and build this turn's set."""
    listings = await asyncio.gather(
        *(_list_tools(s, connect, list_timeout_s) for s in servers), return_exceptions=True
    )
    turn = TurnTools()
    for server, listing in zip(servers, listings, strict=True):
        turn.excluded |= {_tool_name(server.name, t) for t in server.exclude_tools}
        if isinstance(listing, BaseException):
            log.warning(
                "MCP server %s is unavailable this turn: %s", server.name, _describe(listing)
            )
            turn.unavailable.append(server.name)
            continue
        _add_instructions(turn, server, listing.instructions, instructions_max_chars)
        exposed = {t.name for t in listing.tools}
        for unknown in sorted(server.exclude_tools - exposed):
            log.warning(
                "MCP server %s has no tool %r (listed in exclude_tools)", server.name, unknown
            )
        for tool in listing.tools:
            if tool.name in server.exclude_tools:
                continue
            _add(turn, server, tool)
    return turn


async def call_tool(
    turn: TurnTools,
    name: str,
    raw_arguments: str | dict[str, Any] | None,
    *,
    connect: Connector,
    call_timeout_s: float,
    max_chars: int,
) -> ToolOutcome:
    """Run one model tool call. Every failure becomes an error result, never an exception."""
    route = turn.routes.get(name)
    if route is None:
        if name in turn.excluded:
            return ToolOutcome(
                False, f"Error: tool {name!r} is disabled in alfred's configuration."
            )
        return ToolOutcome(False, f"Error: unknown tool {name!r}.")

    arguments = parse_arguments(raw_arguments)
    if isinstance(arguments, str):
        return ToolOutcome(False, arguments)
    return await _send(
        route, name, arguments, _Leg(), connect=connect, call_timeout_s=call_timeout_s,
        max_chars=max_chars,
    )  # fmt: skip


async def resume_tool(
    turn: TurnTools,
    name: str,
    raw_arguments: str | dict[str, Any] | None,
    state: dict[str, Any],
    answer: Any,
    *,
    connect: Connector,
    call_timeout_s: float,
    max_chars: int,
) -> ToolOutcome:
    """Repeat a call the server asked about, with the user's answer.

    ``state`` is what :func:`classify` stored and ``answer`` the chosen
    option's ``ElicitResult``. A server that no longer accepts the saved
    ``request_state`` (expired, restarted, changed arguments) gives the
    late-answer error.
    """
    route = turn.routes.get(name)
    if route is None or state.get("via") != _VIA_MCP:
        return ToolOutcome(False, f"Error: unknown tool {name!r}.")
    arguments = parse_arguments(raw_arguments)
    if isinstance(arguments, str):
        return ToolOutcome(False, arguments)
    leg = _Leg(
        responses={state["key"]: ElicitResult.model_validate(answer)},
        request_state=state["request_state"],
        rounds=state["round"],
    )
    return await _send(
        route, name, arguments, leg, connect=connect, call_timeout_s=call_timeout_s,
        max_chars=max_chars,
    )  # fmt: skip


def _tool_name(server: str, tool: str) -> str:
    """The name the model sees: ``<server>_<tool>`` with disallowed characters as ``_``."""
    return _INVALID_CHARS.sub("_", f"{server}_{tool}")


@dataclass(frozen=True)
class _Listing:
    tools: list[Tool]
    instructions: str | None


def _add_instructions(turn: TurnTools, server: McpServer, text: str | None, max_chars: int) -> None:
    if not server.use_server_instructions or text is None or not text.strip():
        return
    text = text.strip()
    if len(text) > max_chars:
        log.warning(
            "MCP server %s sent %d characters of instructions; cut to %d "
            "(mcp.instructions_max_chars)",
            server.name,
            len(text),
            max_chars,
        )
    turn.instructions.append(ServerInstructions(server.name, _truncate(text, max_chars)))


def _add(turn: TurnTools, server: McpServer, tool: Tool) -> None:
    name = _tool_name(server.name, tool.name)
    if len(name) > _MAX_TOOL_NAME:
        log.warning("skipping %s tool %r: name %r is too long", server.name, tool.name, name)
        return
    if name in turn.routes:
        other = turn.routes[name]
        log.warning(
            "skipping %s tool %r: name %r collides with %s tool %r",
            server.name,
            tool.name,
            name,
            other.server.name,
            other.tool,
        )
        return
    turn.routes[name] = _Route(server, tool.name)
    turn.definitions.append(
        {
            "type": "function",
            "function": {
                "name": name,
                "description": tool.description or "",
                "parameters": tool.input_schema or {"type": "object", "properties": {}},
            },
        }
    )


async def _list_tools(server: McpServer, connect: Connector, limit_s: float) -> _Listing:
    async with asyncio.timeout(limit_s), connect(server, limit_s) as client:
        tools: list[Tool] = []
        cursor: str | None = None
        while True:
            result = await client.list_tools(cursor=cursor)
            tools += result.tools
            cursor = result.next_cursor
            if not cursor:
                # Set by the initialize handshake, so already there inside ``async with``.
                return _Listing(tools, client.instructions)


@dataclass(frozen=True)
class _Leg:
    """What one ``tools/call`` carries back to the server, and how many questions came before."""

    responses: dict[str, ElicitResult] | None = None
    request_state: str | None = None
    rounds: int = 0


async def _send(
    route: _Route,
    name: str,
    arguments: dict[str, Any],
    leg: _Leg,
    *,
    connect: Connector,
    call_timeout_s: float,
    max_chars: int,
) -> ToolOutcome:
    try:
        async with (
            asyncio.timeout(call_timeout_s),
            connect(route.server, call_timeout_s) as client,
        ):
            result = await _call(client, route.tool, arguments, leg.responses, leg.request_state)
            # request_state alone asks nothing of the user: send it straight back.
            for delay in _STATE_ONLY_DELAYS_S:
                if not isinstance(result, InputRequiredResult) or result.input_requests:
                    break
                await asyncio.sleep(delay)
                result = await _call(client, route.tool, arguments, None, result.request_state)
    except TimeoutError:
        return ToolOutcome(False, f"Error: tool {name!r} timed out after {call_timeout_s:g}s.")
    except Exception as exc:  # noqa: BLE001 - any failure is reported to the model
        # Raised inside the connection, so it may come wrapped in an exception group.
        cause = _leaf(exc)
        refused = isinstance(cause, MCPError) and cause.code == INVALID_PARAMS
        if refused and leg.request_state is not None:
            log.warning("tool %s: the server refused the saved request: %s", name, cause)
            return ToolOutcome(False, _late_answer(route.server.name, name))
        log.warning("tool %s failed: %s", name, _describe(exc))
        return ToolOutcome(False, f"Error: tool {name!r} failed: {_describe(exc)}")
    if isinstance(result, InputRequiredResult):
        rounds = leg.rounds + 1
        if rounds > _MAX_INPUT_ROUNDS or not result.input_requests:
            return ToolOutcome(
                False,
                f"Error: {route.server.name} kept asking for input "
                f"({_MAX_INPUT_ROUNDS} questions); the call did not complete.",
            )
        return _classify(route.server.name, name, result, rounds)
    return ToolOutcome(not result.is_error, _truncate(_result_text(result), max_chars))


async def _call(
    client: Client,
    tool: str,
    arguments: dict[str, Any],
    responses: dict[str, ElicitResult] | None,
    request_state: str | None,
) -> CallToolResult | InputRequiredResult:
    """One ``tools/call`` that may come back asking for input.

    The session-level call skips the high-level client's recovery from
    ``HEADER_MISMATCH`` (a tool whose arguments are mirrored in headers,
    called on a connection that hasn't listed it), so it is repeated here:
    list the tools, then send once more.
    """

    async def send() -> CallToolResult | InputRequiredResult:
        return await client.session.call_tool(
            tool,
            arguments,
            input_responses=dict(responses) if responses is not None else None,
            request_state=request_state,
            allow_input_required=True,
        )

    try:
        return await send()
    except MCPError as exc:
        if exc.code != HEADER_MISMATCH:
            raise
    cursor: str | None = None
    while True:
        page = await client.list_tools(cursor=cursor)
        cursor = page.next_cursor
        if not cursor or any(t.name == tool for t in page.tools):
            break
    return await send()


def _late_answer(server: str, name: str) -> str:
    return (
        f"Error: the user's answer could not be used: {server} no longer accepts it "
        f"(it may have expired or the server restarted). {name} did not complete; "
        "you may call it again, which asks the user again."
    )


def _classify(server: str, name: str, result: InputRequiredResult, rounds: int) -> ToolOutcome:
    """A server's question as a need for the user's answer, or why alfred can't show it.

    Shown only as buttons: exactly one form elicitation whose schema has no
    fields (Yes / No), one boolean (Yes / No / Cancel), or one string with at
    most eight choices (one each, and Cancel). Each option's answer is the
    ``ElicitResult`` to send back, as a dict.
    """
    requests = result.input_requests or {}
    if len(requests) != 1:
        return _cant_show(server, name, f"{len(requests)} questions at once")
    ((key, request),) = requests.items()
    if not isinstance(request, ElicitRequest):
        return _cant_show(server, name, f"a {request.method} request")
    params = request.params
    if not isinstance(params, ElicitRequestFormParams):
        return _cant_show(server, name, f"{params.mode} mode")
    options = _options(params.requested_schema)
    if isinstance(options, str):
        return _cant_show(server, name, options)
    question = Question(f"{server} asks: {params.message}", options, detail=name)
    state = {"via": _VIA_MCP, "key": key, "request_state": result.request_state, "round": rounds}
    return ToolOutcome.waiting(InputNeed(question, state))


def _options(schema: Any) -> tuple[Option, ...] | str:
    """The buttons for a form schema, or what about it doesn't fit."""
    properties = (schema.get("properties") or {}) if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return "a form alfred can't read"
    if not properties:
        return (Option(_YES, {"action": "accept", "content": {}}), Option(_NO, _DECLINE))
    if len(properties) > 1:
        return f"{len(properties)} fields"
    ((field_name, spec),) = properties.items()
    spec = spec if isinstance(spec, dict) else {}
    if spec.get("type") == "boolean":
        return (
            Option(_YES, {"action": "accept", "content": {field_name: True}}),
            Option(_NO, {"action": "accept", "content": {field_name: False}}),
            Option(_CANCEL, _DECLINE),
        )
    choices = _choices(spec)
    if choices is None:
        return f"a {spec.get('type', 'free')} field"
    if len(choices) > _MAX_CHOICES:
        return f"{len(choices)} choices"
    return (
        *(Option(label, {"action": "accept", "content": {field_name: v}}) for v, label in choices),
        Option(_CANCEL, _DECLINE),
    )


def _choices(spec: dict[str, Any]) -> list[tuple[str, str]] | None:
    """A string field's (value, label) pairs: from ``enum``, or ``oneOf`` of ``const``/``title``."""
    if spec.get("type") != "string":
        return None
    enum = spec.get("enum")
    if isinstance(enum, list) and enum and all(isinstance(v, str) for v in enum):
        return [(v, v) for v in enum]
    one_of = spec.get("oneOf")
    if isinstance(one_of, list) and one_of:
        pairs = []
        for item in one_of:
            const = item.get("const") if isinstance(item, dict) else None
            if not isinstance(const, str):
                return None
            title = item.get("title")
            pairs.append((const, title if isinstance(title, str) and title else const))
        return pairs
    return None


def _cant_show(server: str, name: str, what: str) -> ToolOutcome:
    log.warning("tool %s: %s asked for input alfred can't show (%s)", name, server, what)
    return ToolOutcome(
        False,
        f"Error: {server} asked for input alfred can't show ({what}); the call did not complete.",
    )


def _result_text(result: CallToolResult) -> str:
    """Structured content as JSON if present, otherwise the text blocks joined."""
    if result.structured_content is not None and not result.is_error:
        return json.dumps(result.structured_content, ensure_ascii=False)
    parts: list[str] = []
    for block in result.content:
        if isinstance(block, TextContent):
            parts.append(block.text)
        else:
            parts.append(f"[{block.type} content omitted]")
    text = "\n".join(parts)
    if result.is_error:
        return f"Error: {text}" if text else "Error: the tool reported an error."
    return text


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n[truncated: {len(text) - max_chars} more characters]"


def _leaf(exc: BaseException) -> BaseException:
    """The first exception inside nested exception groups."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def _describe(exc: BaseException) -> str:
    """A short description of a failure, unwrapping exception groups."""
    exc = _leaf(exc)
    if isinstance(exc, TimeoutError):
        return "timed out"
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__
