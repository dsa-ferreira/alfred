from __future__ import annotations

import json

import httpx
import pytest

from alfred.memory.embedding import EmbeddingClient, EmbeddingFailed, EmbeddingUnreachable

URL = "http://ragman.lan/v1/embeddings"


def client(handler, **kwargs) -> EmbeddingClient:  # type: ignore[no-untyped-def]
    return EmbeddingClient(
        "http://ragman.lan/",
        "nomic-embed-text:v1.5",
        30,
        document_prefix="search_document: ",
        query_prefix="search_query: ",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def vector_reply(vector: list[float]) -> httpx.Response:
    return httpx.Response(200, json={"object": "list", "data": [{"embedding": vector}]})


async def test_success_and_request_shape() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return vector_reply([0.5, -1, 2.25])

    embedder = client(handler)
    assert await embedder.embed_document("Chicken. Lemon and garlic.") == [0.5, -1.0, 2.25]
    await embedder.aclose()
    assert str(seen[0].url) == URL
    assert json.loads(seen[0].content) == {
        "model": "nomic-embed-text:v1.5",
        "input": "search_document: Chicken. Lemon and garlic.",
    }


async def test_query_prefix() -> None:
    inputs: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        inputs.append(json.loads(request.content)["input"])
        return vector_reply([1.0])

    embedder = client(handler)
    await embedder.embed_query("chicken recipe")
    await embedder.aclose()
    assert inputs == ["search_query: chicken recipe"]


async def test_connection_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    embedder = client(handler)
    with pytest.raises(EmbeddingUnreachable, match="can't reach"):
        await embedder.embed_query("x")
    await embedder.aclose()


async def test_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    embedder = client(handler)
    with pytest.raises(EmbeddingUnreachable, match="timed out"):
        await embedder.embed_query("x")
    await embedder.aclose()


async def test_server_error() -> None:
    embedder = client(lambda request: httpx.Response(503, text="loading"))
    with pytest.raises(EmbeddingUnreachable, match="HTTP 503"):
        await embedder.embed_query("x")
    await embedder.aclose()


async def test_client_error() -> None:
    embedder = client(lambda request: httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(EmbeddingFailed, match="HTTP 404.*model not found"):
        await embedder.embed_query("x")
    await embedder.aclose()


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        json.dumps({"data": []}),
        json.dumps({"data": [{"embedding": []}]}),
        json.dumps({"data": [{"embedding": ["a", "b"]}]}),
        json.dumps({"data": [{"no_embedding": [1]}]}),
        json.dumps([1, 2, 3]),
    ],
)
async def test_malformed_reply(body: str) -> None:
    embedder = client(lambda request: httpx.Response(200, text=body))
    with pytest.raises(EmbeddingFailed, match="unusable embedding reply"):
        await embedder.embed_query("x")
    await embedder.aclose()
