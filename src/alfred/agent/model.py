"""OpenAI-compatible chat completions client over httpx (design D4).

Failures are classified because that decides retrying:

- :class:`ModelUnreachable`: connection errors, timeouts and 5xx answers. The
  AI server may be asleep or restarting; the turn can be retried if no tool
  has run yet.
- :class:`ModelFailed`: 4xx answers and replies that can't be understood.
  Retrying would fail the same way.

Summaries use a streamed request (:meth:`ModelClient.complete_streamed`):
Ollama stops generating when a streamed request's client goes away, but
finishes a non-streamed one regardless (change close-idle-conversations,
design D6).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx


class ModelError(Exception):
    """A model request that produced no usable answer. Messages are safe to log."""


class ModelUnreachable(ModelError):
    pass


class ModelFailed(ModelError):
    pass


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class _Usage:
    """Token counts the model server reported for one request."""

    prompt_tokens: int
    completion_tokens: int


def token_counts(usage: _Usage | None) -> str:
    """The counts for a log line, or a note that the server sent none."""
    if usage is None:
        return "token counts not reported"
    return f"{usage.prompt_tokens} prompt tokens, {usage.completion_tokens} completion tokens"


@dataclass(frozen=True)
class ModelReply:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    # None when the server reported no (usable) token counts.
    usage: _Usage | None = None

    def assistant_message(self) -> dict[str, Any]:
        """The reply as a message to send back in the next request."""
        message: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments},
                }
                for c in self.tool_calls
            ]
        return message


class ModelClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_s: float,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/v1/chat/completions"
        self._model = model
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s), transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        model: str | None = None,
    ) -> ModelReply:
        """One request; ``model`` replaces the client's model for this request only."""
        body: dict[str, Any] = {
            "model": model or self._model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
        try:
            response = await self._http.post(self._url, json=body)
        except httpx.TimeoutException as exc:
            raise ModelUnreachable(f"model request timed out ({type(exc).__name__})") from None
        except httpx.TransportError as exc:
            raise ModelUnreachable(f"can't reach the model server ({type(exc).__name__})") from None

        status = response.status_code
        if status >= 500:
            raise ModelUnreachable(f"model server answered HTTP {status}")
        if status != 200:
            raise ModelFailed(f"model server answered HTTP {status}: {_snippet(response.text)}")
        try:
            return _parse(response.json())
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelFailed(f"unusable model reply: {exc}") from None

    async def complete_streamed(
        self, messages: list[dict[str, Any]], *, model: str | None = None
    ) -> ModelReply:
        """One streamed request with no tools, joined into a single reply.

        Same errors as :meth:`complete`. Cancelling the call closes the
        connection, which makes the server stop generating.
        """
        body: dict[str, Any] = {
            "model": model or self._model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        try:
            async with self._http.stream("POST", self._url, json=body) as response:
                status = response.status_code
                if status >= 500:
                    raise ModelUnreachable(f"model server answered HTTP {status}")
                if status != 200:
                    await response.aread()
                    raise ModelFailed(
                        f"model server answered HTTP {status}: {_snippet(response.text)}"
                    )
                chunks = [chunk async for chunk in _sse_data(response)]
        except httpx.TimeoutException as exc:
            raise ModelUnreachable(f"model request timed out ({type(exc).__name__})") from None
        except httpx.TransportError as exc:
            raise ModelUnreachable(f"can't reach the model server ({type(exc).__name__})") from None
        try:
            return _join(chunks)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelFailed(f"unusable model reply: {exc}") from None


async def _sse_data(response: httpx.Response) -> AsyncIterator[str]:
    """The ``data:`` payloads of a server-sent event stream, up to ``[DONE]``."""
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue  # blank separators, comments and other fields
        data = line[5:].strip()
        if data == "[DONE]":
            return
        yield data


def _join(chunks: list[str]) -> ModelReply:
    """The streamed chunks as one reply: their content joined, usage from the last."""
    parts: list[str] = []
    usage: _Usage | None = None
    seen_choice = False
    for raw in chunks:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("a chunk is not an object")
        if "error" in data:
            raise ValueError(f"error in the stream: {data['error']}")
        for choice in data.get("choices") or []:
            seen_choice = True
            delta = choice["delta"]
            if not isinstance(delta, dict):
                raise ValueError("choices[0].delta is not an object")
            content = delta.get("content")
            if content is not None and not isinstance(content, str):
                raise ValueError("delta content is not text")
            if content:
                parts.append(content)
        if data.get("usage") is not None:
            usage = _usage(data["usage"])
    if not seen_choice:
        raise ValueError("no choices in the reply")
    return ModelReply(content="".join(parts), usage=usage)


def _parse(data: Any) -> ModelReply:
    if not isinstance(data, dict) or not data.get("choices"):
        raise ValueError("no choices in the reply")
    message = data["choices"][0]["message"]
    if not isinstance(message, dict):
        raise ValueError("choices[0].message is not an object")
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise ValueError("message content is not text")
    calls: list[ToolCall] = []
    for i, raw in enumerate(message.get("tool_calls") or []):
        function = raw["function"]
        arguments = function.get("arguments")
        if isinstance(arguments, dict):  # some servers send an object, not a JSON string
            arguments = json.dumps(arguments)
        calls.append(
            ToolCall(
                id=str(raw.get("id") or f"call_{i}"),
                name=str(function["name"]),
                arguments=arguments if isinstance(arguments, str) else "",
            )
        )
    return ModelReply(content=content, tool_calls=calls, usage=_usage(data.get("usage")))


def _usage(raw: Any) -> _Usage | None:
    """The reply's token counts, or ``None``; a missing or odd ``usage`` is never an error."""
    if not isinstance(raw, dict):
        return None
    prompt, completion = raw.get("prompt_tokens"), raw.get("completion_tokens")
    if not all(isinstance(n, int) and not isinstance(n, bool) for n in (prompt, completion)):
        return None
    return _Usage(prompt_tokens=prompt, completion_tokens=completion)


def _snippet(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"
