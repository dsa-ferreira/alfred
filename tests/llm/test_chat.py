from __future__ import annotations

import httpx
import pytest
import respx

from alfred.llm.chat import ModelClient, ModelFailed, ModelUnreachable, _Usage

URL = "http://ai.lan/v1/chat/completions"


def reply(message: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": message}]})


@respx.mock
async def test_content_reply_and_request_shape() -> None:
    route = respx.post(URL).mock(return_value=reply({"role": "assistant", "content": "hi"}))
    client = ModelClient("http://ai.lan", "gemma4:12b-it-qat", 300)
    tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
    result = await client.complete([{"role": "user", "content": "hello"}], tools)
    assert result.content == "hi" and result.tool_calls == []
    body = __import__("json").loads(route.calls.last.request.content)
    assert body["model"] == "gemma4:12b-it-qat"
    assert body["stream"] is False
    assert body["tools"] == tools
    await client.aclose()


@respx.mock
async def test_model_chosen_per_request() -> None:
    route = respx.post(URL).mock(return_value=reply({"content": "ok"}))
    client = ModelClient("http://ai.lan", "gemma4:26b-a4b-it-qat", 300)
    await client.complete([], [], model="bonsai:8b")
    await client.complete([], [])
    models = [__import__("json").loads(c.request.content)["model"] for c in route.calls]
    assert models == ["bonsai:8b", "gemma4:26b-a4b-it-qat"]
    await client.aclose()


@respx.mock
async def test_switch_servers() -> None:
    route = respx.post("https://other.example/v1/chat/completions").mock(
        return_value=reply({"content": "ok"})
    )
    client = ModelClient("https://other.example/", "other-model", 300)
    await client.complete([], [])
    assert route.called
    await client.aclose()


@respx.mock
async def test_tool_call_reply() -> None:
    respx.post(URL).mock(
        return_value=reply(
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "mcparr_get_downloads", "arguments": "{}"},
                    },
                    {"function": {"name": "x", "arguments": {"a": 1}}},
                ],
            }
        )
    )
    client = ModelClient("http://ai.lan", "m", 300)
    result = await client.complete([], [])
    assert [(c.id, c.name, c.arguments) for c in result.tool_calls] == [
        ("c1", "mcparr_get_downloads", "{}"),
        ("call_1", "x", '{"a": 1}'),
    ]
    assert result.assistant_message()["tool_calls"][0]["function"]["name"] == (
        "mcparr_get_downloads"
    )
    await client.aclose()


@respx.mock
@pytest.mark.parametrize(
    "outcome",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.Response(503),
        httpx.Response(502),
        httpx.Response(500),
    ],
    ids=["connect", "timeout", "503", "502", "500"],
)
async def test_unreachable(outcome: object) -> None:
    if isinstance(outcome, Exception):
        respx.post(URL).mock(side_effect=outcome)
    else:
        respx.post(URL).mock(return_value=outcome)
    client = ModelClient("http://ai.lan", "m", 300)
    with pytest.raises(ModelUnreachable):
        await client.complete([], [])
    await client.aclose()


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error": "bad request"}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"id": "x"}),
        httpx.Response(200, json={"choices": []}),
    ],
    ids=["400", "malformed-json", "missing-choices", "empty-choices"],
)
async def test_failed(response: httpx.Response) -> None:
    respx.post(URL).mock(return_value=response)
    client = ModelClient("http://ai.lan", "m", 300)
    with pytest.raises(ModelFailed):
        await client.complete([], [])
    await client.aclose()


def test_default_timeout_allows_a_slow_cold_start() -> None:
    from alfred.config import _AIConfig

    client = ModelClient("http://ai.lan", "m", _AIConfig().request_timeout_s)
    # A 90-second wake-up must fit in every phase of the request.
    assert client._http.timeout.read >= 90  # noqa: SLF001
    assert client._http.timeout.connect >= 90  # noqa: SLF001


@respx.mock
@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"prompt_tokens": 6812, "completion_tokens": 38, "total_tokens": 6850}, _Usage(6812, 38)),
        (None, None),
        ({"prompt_tokens": "lots", "completion_tokens": 38}, None),
        ({"prompt_tokens": 10}, None),
        ([1, 2], None),
    ],
    ids=["reported", "missing", "not-a-number", "incomplete", "not-an-object"],
)
async def test_usage(usage: object, expected: _Usage | None) -> None:
    data: dict = {"choices": [{"message": {"content": "hi"}}]}
    if usage is not None:
        data["usage"] = usage
    respx.post(URL).mock(return_value=httpx.Response(200, json=data))
    client = ModelClient("http://ai.lan", "m", 300)
    result = await client.complete([], [])
    assert result.content == "hi"
    assert result.usage == expected
    await client.aclose()


# --- streamed requests, for summaries (change close-idle-conversations, D6) ------


def sse(*events: object, done: bool = True) -> bytes:
    """An SSE body: one ``data:`` event per item (dicts as JSON, strings as is)."""
    import json

    lines = [f"data: {e if isinstance(e, str) else json.dumps(e)}\n\n" for e in events]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def delta(content: str) -> dict:
    return {"choices": [{"index": 0, "delta": {"content": content}}]}


USAGE_CHUNK = {"choices": [], "usage": {"prompt_tokens": 812, "completion_tokens": 41}}


def streaming(body: bytes | Exception, status: int = 200, seen: list | None = None) -> ModelClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if isinstance(body, Exception):
            raise body
        return httpx.Response(status, content=body, headers={"content-type": "text/event-stream"})

    return ModelClient("http://ai.lan", "gemma", 300, transport=httpx.MockTransport(handler))


async def test_streamed_reply_is_joined() -> None:
    import json

    seen: list[httpx.Request] = []
    body = sse(
        {"choices": [{"delta": {"role": "assistant"}}]},
        delta("Title: "),
        delta("Soup"),
        USAGE_CHUNK,
    )
    client = streaming(body, seen=seen)
    result = await client.complete_streamed([{"role": "user", "content": "sum up"}])
    assert result.content == "Title: Soup"
    assert result.tool_calls == []
    assert result.usage == _Usage(prompt_tokens=812, completion_tokens=41)
    sent = json.loads(seen[0].content)
    assert sent["stream"] is True
    assert sent["stream_options"] == {"include_usage": True}
    assert "tools" not in sent and sent["model"] == "gemma"
    await client.aclose()


async def test_streamed_reply_without_usage() -> None:
    client = streaming(sse(delta("ok"), done=False))
    result = await client.complete_streamed([])
    assert result.content == "ok" and result.usage is None
    await client.aclose()


@pytest.mark.parametrize(
    "outcome",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.ReadError("reset")],
)
async def test_streamed_unreachable(outcome: Exception) -> None:
    client = streaming(outcome)
    with pytest.raises(ModelUnreachable):
        await client.complete_streamed([])
    await client.aclose()


async def test_streamed_5xx_is_unreachable() -> None:
    client = streaming(b"loading model", status=503)
    with pytest.raises(ModelUnreachable, match="HTTP 503"):
        await client.complete_streamed([])
    await client.aclose()


async def test_streamed_4xx_fails() -> None:
    client = streaming(b'{"error": "model not found"}', status=404)
    with pytest.raises(ModelFailed, match="HTTP 404: .*model not found"):
        await client.complete_streamed([])
    await client.aclose()


@pytest.mark.parametrize(
    "body",
    [
        sse("{not json"),
        sse({"choices": [{"delta": "text"}]}),
        sse({"choices": [{"delta": {"content": 7}}]}),
        sse({"error": {"message": "out of memory"}}),
        sse(USAGE_CHUNK),  # no choices at all
        b"",
    ],
)
async def test_streamed_malformed_fails_like_a_plain_reply(body: bytes) -> None:
    client = streaming(body)
    with pytest.raises(ModelFailed, match="unusable model reply"):
        await client.complete_streamed([])
    await client.aclose()
