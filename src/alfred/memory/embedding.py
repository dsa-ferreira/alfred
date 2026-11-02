"""OpenAI-compatible embeddings client over httpx (memory design D6).

Failures are classified like :mod:`alfred.agent.model`'s:

- :class:`EmbeddingUnreachable`: connection errors, timeouts and 5xx answers.
- :class:`EmbeddingFailed`: 4xx answers and replies that can't be understood.

Either way the caller carries on without the vector: search falls back to
keywords, and the sweeper embeds the conversation later.
"""

from __future__ import annotations

from typing import Any

import httpx


class EmbeddingError(Exception):
    """An embedding request that produced no vector. Messages are safe to log."""


class EmbeddingUnreachable(EmbeddingError):
    pass


class EmbeddingFailed(EmbeddingError):
    pass


class EmbeddingClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_s: float,
        *,
        document_prefix: str = "",
        query_prefix: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/v1/embeddings"
        self.model = model
        self._document_prefix = document_prefix
        self._query_prefix = query_prefix
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s), transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def embed_document(self, text: str) -> list[float]:
        """The vector stored for a conversation's title and summary."""
        return await self._embed(self._document_prefix + text)

    async def embed_query(self, text: str) -> list[float]:
        """The vector a search query is compared with."""
        return await self._embed(self._query_prefix + text)

    async def _embed(self, text: str) -> list[float]:
        body = {"model": self.model, "input": text}
        try:
            response = await self._http.post(self._url, json=body)
        except httpx.TimeoutException as exc:
            raise EmbeddingUnreachable(
                f"embedding request timed out ({type(exc).__name__})"
            ) from None
        except httpx.TransportError as exc:
            raise EmbeddingUnreachable(
                f"can't reach the embedding server ({type(exc).__name__})"
            ) from None

        status = response.status_code
        if status >= 500:
            raise EmbeddingUnreachable(f"embedding server answered HTTP {status}")
        if status != 200:
            raise EmbeddingFailed(f"embedding server answered HTTP {status}: {_snippet(response)}")
        try:
            return _parse(response.json())
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise EmbeddingFailed(f"unusable embedding reply: {exc}") from None


def _parse(data: Any) -> list[float]:
    if not isinstance(data, dict) or not data.get("data"):
        raise ValueError("no data in the reply")
    vector = data["data"][0]["embedding"]
    if not isinstance(vector, list) or not vector:
        raise ValueError("data[0].embedding is not a list of numbers")
    if not all(isinstance(x, int | float) and not isinstance(x, bool) for x in vector):
        raise ValueError("data[0].embedding is not a list of numbers")
    return [float(x) for x in vector]


def _snippet(response: httpx.Response, limit: int = 200) -> str:
    text = " ".join(response.text.split())
    return text if len(text) <= limit else text[:limit] + "…"
