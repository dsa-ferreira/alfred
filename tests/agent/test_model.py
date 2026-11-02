from __future__ import annotations

import httpx
import pytest
import respx

from alfred.agent.model import ModelClient, ModelFailed, ModelUnreachable, Usage

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
    from alfred.config import AIConfig

    client = ModelClient("http://ai.lan", "m", AIConfig().request_timeout_s)
    # A 90-second wake-up must fit in every phase of the request.
    assert client._http.timeout.read >= 90  # noqa: SLF001
    assert client._http.timeout.connect >= 90  # noqa: SLF001


@respx.mock
@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"prompt_tokens": 6812, "completion_tokens": 38, "total_tokens": 6850}, Usage(6812, 38)),
        (None, None),
        ({"prompt_tokens": "lots", "completion_tokens": 38}, None),
        ({"prompt_tokens": 10}, None),
        ([1, 2], None),
    ],
    ids=["reported", "missing", "not-a-number", "incomplete", "not-an-object"],
)
async def test_usage(usage: object, expected: Usage | None) -> None:
    data: dict = {"choices": [{"message": {"content": "hi"}}]}
    if usage is not None:
        data["usage"] = usage
    respx.post(URL).mock(return_value=httpx.Response(200, json=data))
    client = ModelClient("http://ai.lan", "m", 300)
    result = await client.complete([], [])
    assert result.content == "hi"
    assert result.usage == expected
    await client.aclose()
