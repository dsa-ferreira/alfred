"""OpenAI-compatible chat completions client over httpx (design D4).

Failures are classified because that decides retrying:

- :class:`ModelUnreachable`: connection errors, timeouts and 5xx answers. The
  AI server may be asleep or restarting; the turn can be retried if no tool
  has run yet.
- :class:`ModelFailed`: 4xx answers and replies that can't be understood.
  Retrying would fail the same way.
"""

from __future__ import annotations

import json
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
class Usage:
    """Token counts the model server reported for one request."""

    prompt_tokens: int
    completion_tokens: int


def token_counts(usage: Usage | None) -> str:
    """The counts for a log line, or a note that the server sent none."""
    if usage is None:
        return "token counts not reported"
    return f"{usage.prompt_tokens} prompt tokens, {usage.completion_tokens} completion tokens"


@dataclass(frozen=True)
class ModelReply:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    # None when the server reported no (usable) token counts.
    usage: Usage | None = None

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
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply:
        body: dict[str, Any] = {"model": self._model, "messages": messages, "stream": False}
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


def _usage(raw: Any) -> Usage | None:
    """The reply's token counts, or ``None``; a missing or odd ``usage`` is never an error."""
    if not isinstance(raw, dict):
        return None
    prompt, completion = raw.get("prompt_tokens"), raw.get("completion_tokens")
    if not all(isinstance(n, int) and not isinstance(n, bool) for n in (prompt, completion)):
        return None
    return Usage(prompt_tokens=prompt, completion_tokens=completion)


def _snippet(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"
