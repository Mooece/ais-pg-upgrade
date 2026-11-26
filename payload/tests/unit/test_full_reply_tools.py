"""Serial client tool turns alongside the private full-reply envelope."""

import asyncio
import json
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from aistudio_api.api import routes_openai as routes
from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.reply_cache import get_reply_cache
from aistudio_api.api.schemas import OpenAIChatCompletionRequest
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.client import AIStudioClient

MODEL = "gemini-4-argon[防截断]"


def declaration(name="context"):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Synthetic preset tool",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }


def tool_reply(name="context", *, reason="STOP", args=None):
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"text": "private reasoning", "thought": True},
                        {
                            "functionCall": {
                                "name": name,
                                "id": "preset-call",
                                "args": {"query": "测试"} if args is None else args,
                            },
                            "thoughtSignature": "opaque-test-signature",
                        },
                    ],
                },
                "finishReason": reason,
            }
        ],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 7},
    }


def final_reply(name="aistudio_full_reply"):
    return tool_reply(name, args={"content": "完整最终回答。"})


def response_completion(response, stream):
    if not stream:
        result = response.json()
        for choice in result.get("choices", []):
            choice.pop("index", None)
        return result
    events = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert not any("error" in event for event in events)
    assert response.text.count("data: [DONE]") == 1
    message = {"role": "assistant", "content": None}
    modes = []
    finish = None
    for event in events:
        if "aistudio_full_reply" in event:
            modes.append(event["aistudio_full_reply"])
        for choice in event.get("choices", []):
            delta = choice["delta"]
            if "content" in delta:
                message["content"] = (message["content"] or "") + delta["content"]
            if "tool_calls" in delta:
                message["tool_calls"] = [
                    {key: value for key, value in call.items() if key != "index"}
                    for call in delta["tool_calls"]
                ]
            if choice["finish_reason"] is not None:
                finish = choice["finish_reason"]
    return {
        "choices": [{"message": message, "finish_reason": finish}],
        "aistudio_full_reply": modes[0],
    }


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", frozenset())
    app.dependency_overrides[get_client] = lambda: MagicMock(spec=AIStudioClient)
    yield
    app.dependency_overrides.pop(get_client, None)
    app.dependency_overrides.pop(routes._reply_cache_owner, None)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("buffered_body", [False, True])
@pytest.mark.parametrize("name", ["context", "aistudio_full_reply"])
async def test_preset_tool_result_roundtrip_then_private_final_answer(
    api, monkeypatch, stream, buffered_body, name
):
    async def handle(model, request, client, **kwargs):
        assert kwargs == {"stream": False, "require_finish_reason": True}
        assert model == "gemini-4-argon"
        envelope = request.tools[-1].functionDeclarations[-1]["name"]
        assert envelope != name
        assert request.tools[0].functionDeclarations[0] == declaration(name)["function"]
        response = (
            tool_reply(name) if len(request.contents) == 1 else final_reply(envelope)
        )
        if not buffered_body:
            return response

        async def body():
            encoded = json.dumps(response, ensure_ascii=False).encode()
            yield b"\n"
            for offset in range(0, len(encoded), 3):
                yield encoded[offset : offset + 3]

        return StreamingResponse(body())

    upstream = AsyncMock(side_effect=handle)
    monkeypatch.setattr(routes, "handle_gemini_generate_content", upstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        messages = [{"role": "user", "content": "Use the preset tool"}]
        first = await client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": messages,
                "tools": [declaration(name)],
                "stream": stream,
            },
        )
        assert first.status_code == 200
        completion = response_completion(first, stream)
        choice = completion["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        assert completion["aistudio_full_reply"] == {"mode": "client_tool_call"}
        tool_call = choice["message"]["tool_calls"][0]
        assert tool_call["function"]["name"] == name
        assert json.loads(tool_call["function"]["arguments"]) == {"query": "测试"}
        assert tool_call["id"].startswith("preset-call--sig-")
        # The gateway returned once; only this synthetic client supplies a result.
        assert upstream.await_count == 1
        messages.extend(
            [
                choice["message"],
                {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": "synthetic result",
                },
            ]
        )
        second = await client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": messages,
                "tools": [declaration(name)],
                "stream": stream,
            },
        )
    final = response_completion(second, stream)
    assert final["choices"][0] == {
        "message": {"role": "assistant", "content": "完整最终回答。"},
        "finish_reason": "stop",
    }
    assert final["aistudio_full_reply"] == {"mode": "function_envelope"}
    followup = upstream.call_args.args[1]
    call = followup.contents[1].parts[0]
    assert call.functionCall.id == "preset-call"
    assert call.thoughtSignature == "opaque-test-signature"
    result = followup.contents[2].parts[0].functionResponse
    assert result.id == "preset-call" and result.name == name
    assert result.response == {"result": "synthetic result"}


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "choice,allowed,response_name",
    [
        (None, ["context", "aistudio_full_reply"], "context"),
        ("auto", ["context", "aistudio_full_reply"], "context"),
        ("none", ["aistudio_full_reply"], "aistudio_full_reply"),
        ("required", ["context"], "context"),
        ({"type": "function", "function": {"name": "context"}}, ["context"], "context"),
    ],
)
async def test_tool_choice_is_applied_to_upstream_and_response(
    api, monkeypatch, stream, choice, allowed, response_name
):
    upstream = AsyncMock(
        return_value=(final_reply() if choice == "none" else tool_reply(response_name))
    )
    monkeypatch.setattr(routes, "handle_gemini_generate_content", upstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "test"}],
                "tools": [declaration()],
                "tool_choice": choice,
                "stream": stream,
            },
        )
    assert response.status_code == 200
    converted = response_completion(response, stream)
    assert converted["choices"][0]["finish_reason"] == (
        "stop" if choice == "none" else "tool_calls"
    )
    assert upstream.call_args.args[1].toolConfig == {
        "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": allowed}
    }


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "choice",
    ["none", "required", {"type": "function", "function": {"name": "context"}}],
)
async def test_choice_violation_never_emits_successful_tool_or_final(
    api, monkeypatch, stream, choice
):
    upstream = AsyncMock(
        return_value=tool_reply() if choice == "none" else final_reply()
    )
    monkeypatch.setattr(routes, "handle_gemini_generate_content", upstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "test"}],
                "tools": [declaration()],
                "tool_choice": choice,
                "stream": stream,
            },
        )
    if stream:
        assert '"error"' in response.text
        assert '"tool_calls"' not in response.text
        assert '"finish_reason":"stop"' not in response.text
    else:
        assert response.status_code == 502


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "failure",
    [
        "missing_finish",
        "MAX_TOKENS",
        "SAFETY",
        "bad_args",
        "multiple",
        "mixed",
        "unknown",
    ],
)
async def test_invalid_tool_reply_is_rejected_before_client_execution(
    api, monkeypatch, stream, failure
):
    payload = tool_reply()
    candidate = payload["candidates"][0]
    parts = candidate["content"]["parts"]
    if failure == "missing_finish":
        candidate.pop("finishReason")
    elif failure in ("MAX_TOKENS", "SAFETY"):
        candidate["finishReason"] = failure
    elif failure == "bad_args":
        parts[-1]["functionCall"]["args"] = '{"query":'
    elif failure == "multiple":
        parts.append(parts[-1])
    elif failure == "mixed":
        parts.extend(final_reply()["candidates"][0]["content"]["parts"])
    elif failure == "unknown":
        parts[-1]["functionCall"]["name"] = "unknown"
    upstream = AsyncMock(return_value=payload)
    monkeypatch.setattr(routes, "handle_gemini_generate_content", upstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "test"}],
                "tools": [declaration()],
                "stream": stream,
            },
        )
    assert upstream.await_count == 1
    if stream:
        assert '"error"' in response.text
        assert '"tool_calls"' not in response.text
    else:
        assert response.status_code == 502


@pytest.mark.parametrize("stream", [False, True])
async def test_armed_cache_does_not_replace_tool_request_or_store_tool_turn(
    api, monkeypatch, stream
):
    cache = get_reply_cache()
    await cache.configure(2)
    await cache.save(
        "tool-owner",
        {
            "model": MODEL,
            "choices": [
                {"message": {"content": "old answer"}, "finish_reason": "stop"}
            ],
        },
    )
    entry = (await cache.status())["entries"][0]
    await cache.arm(entry["id"])
    before = await cache.status()
    app.dependency_overrides[routes._reply_cache_owner] = lambda: "tool-owner"
    upstream = AsyncMock(return_value=tool_reply())
    monkeypatch.setattr(routes, "handle_gemini_generate_content", upstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "test"}],
                "tools": [declaration()],
                "tool_choice": "required",
                "stream": stream,
            },
        )
    assert (
        response_completion(response, stream)["choices"][0]["finish_reason"]
        == "tool_calls"
    )
    assert "X-Aistudio-Reply-Cache" not in response.headers
    assert await cache.status() == before
    assert upstream.await_count == 1


async def test_old_tool_cache_entry_cannot_replay_to_alias_without_tools(
    api, monkeypatch
):
    cache = get_reply_cache()
    await cache.configure(1)
    old = routes.gemini_response_to_openai(tool_reply(), model="gemini-4-argon")
    old["model"] = MODEL
    await cache.save("tool-owner", old)
    await cache.arm((await cache.status())["entries"][0]["id"])
    app.dependency_overrides[routes._reply_cache_owner] = lambda: "tool-owner"
    upstream = AsyncMock(return_value=final_reply())
    monkeypatch.setattr(routes, "handle_gemini_generate_content", upstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "test"}],
            },
        )
    assert response.json()["choices"][0]["finish_reason"] == "stop"
    assert "X-Aistudio-Reply-Cache" not in response.headers
    upstream.assert_awaited_once()


def test_alias_history_ignores_other_requests_raw_id_signature(monkeypatch):
    monkeypatch.setattr(
        routes, "_TOOL_CALL_SIGNATURES", {"preset-call": "unrelated-signature"}
    )
    request = OpenAIChatCompletionRequest(
        model=MODEL,
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "preset-call",
                        "type": "function",
                        "function": {"name": "context", "arguments": "{}"},
                    }
                ],
            }
        ],
        tools=[declaration()],
    )
    part = routes.openai_request_to_gemini(request).contents[0].parts[0]
    assert part.thoughtSignature == "skip_thought_signature_validator"


def tool_history():
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "preset-call",
                    "type": "function",
                    "function": {"name": "context", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "preset-call", "content": "result"},
    ]


@pytest.mark.parametrize(
    "arguments", [None, "null", "1", "[]", '{"value":NaN}', '{"query":']
)
def test_alias_rejects_incomplete_or_nonobject_tool_history(arguments):
    messages = tool_history()
    messages[0]["tool_calls"][0]["function"]["arguments"] = arguments
    with pytest.raises(HTTPException) as exc:
        routes.openai_request_to_gemini(
            OpenAIChatCompletionRequest(model=MODEL, messages=messages)
        )
    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "failure",
    ["missing_call_id", "unknown_result_id", "missing_result_id", "wrong_result_name"],
)
def test_alias_rejects_unpaired_tool_history(failure):
    messages = tool_history()
    if failure == "missing_call_id":
        messages[0]["tool_calls"][0].pop("id")
    elif failure == "unknown_result_id":
        messages[1]["tool_call_id"] = "other-call"
    elif failure == "missing_result_id":
        messages[1].pop("tool_call_id")
        messages[1]["name"] = "context"
    else:
        messages[1]["name"] = "other-name"
    with pytest.raises(HTTPException) as exc:
        routes.openai_request_to_gemini(
            OpenAIChatCompletionRequest(model=MODEL, messages=messages)
        )
    assert exc.value.status_code == 400


@pytest.mark.parametrize("stream", [False, True])
async def test_required_is_respected_again_after_tool_result(api, monkeypatch, stream):
    upstream = AsyncMock(return_value=final_reply())
    monkeypatch.setattr(routes, "handle_gemini_generate_content", upstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": tool_history(),
                "tools": [declaration()],
                "tool_choice": "required",
                "stream": stream,
            },
        )
    assert upstream.call_args.args[1].toolConfig == {
        "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["context"]}
    }
    if stream:
        assert (
            '"error"' in response.text and '"finish_reason":"stop"' not in response.text
        )
    else:
        assert response.status_code == 502


@pytest.mark.parametrize("choice", [None, "auto", "none"])
async def test_alias_can_use_private_envelope_without_client_declarations(
    api, monkeypatch, choice
):
    upstream = AsyncMock(return_value=final_reply())
    monkeypatch.setattr(routes, "handle_gemini_generate_content", upstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "test"}],
                "tool_choice": choice,
            },
        )
    assert response.status_code == 200
    assert response.json()["choices"][0]["finish_reason"] == "stop"
    assert upstream.call_args.args[1].toolConfig == {
        "functionCallingConfig": {
            "mode": "ANY",
            "allowedFunctionNames": ["aistudio_full_reply"],
        }
    }


async def test_tool_stream_buffers_until_complete_arguments_and_finish(
    api, monkeypatch
):
    started = asyncio.Event()
    release = asyncio.Event()
    encoded = json.dumps(tool_reply()).encode()

    async def body():
        yield encoded[: len(encoded) // 2]
        started.set()
        await release.wait()
        yield encoded[len(encoded) // 2 :]

    upstream = AsyncMock(return_value=StreamingResponse(body()))
    monkeypatch.setattr(routes, "handle_gemini_generate_content", upstream)
    request = OpenAIChatCompletionRequest(
        model=MODEL,
        messages=[{"role": "user", "content": "test"}],
        tools=[declaration()],
        stream=True,
    )
    response = await routes.create_chat_completion(
        request, MagicMock(spec=AIStudioClient), None
    )
    assert isinstance(response, StreamingResponse)
    iterator = response.body_iterator
    assert isinstance(iterator, AsyncGenerator)
    pending = asyncio.create_task(anext(iterator))
    try:
        await started.wait()
        await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        first = await pending
        remaining = [chunk async for chunk in iterator]
    finally:
        release.set()
        await iterator.aclose()
    text = str(first) + "".join(str(chunk) for chunk in remaining)
    assert '"arguments":"{\\"query\\":\\"测试\\"}"' in text
    assert '"finish_reason":"tool_calls"' in text
