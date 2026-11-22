"""OpenAI-facing behavior of the opt-in full-reply tool."""

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.responses import StreamingResponse

from aistudio_api.api import routes_openai as routes
from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.application import api_service_gemini
from aistudio_api.config import settings
from aistudio_api.domain.models import Candidate, ModelOutput
from aistudio_api.infrastructure.gateway.client import AIStudioClient, PreparedRequest
from aistudio_api.infrastructure.gateway.session import GatewaySession
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", frozenset())
    app.dependency_overrides[get_client] = lambda: MagicMock(spec=AIStudioClient)
    yield
    app.dependency_overrides.pop(get_client, None)


def full_reply(*, reason="STOP", args=None, name="aistudio_full_reply"):
    return {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "private reasoning", "thought": True},
                        {
                            "functionCall": {
                                "name": name,
                                "args": args
                                if args is not None
                                else {"content": "完整回答\n第二段"},
                            }
                        },
                    ]
                },
                "finishReason": reason,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 3,
            "candidatesTokenCount": 7,
            "totalTokenCount": 10,
        },
    }


@pytest.mark.asyncio
async def test_selected_alias_returns_complete_prose_and_preserves_system(
    api, monkeypatch
):
    upstream = AsyncMock(return_value=full_reply())
    monkeypatch.setattr(routes, "handle_gemini_generate_content", upstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.7-flash[防截断]",
                "messages": [
                    {"role": "system", "content": "用中文回答"},
                    {"role": "user", "content": "继续故事"},
                ],
            },
        )
    assert response.status_code == 200
    result = response.json()
    assert result["choices"][0] == {
        "index": 0,
        "message": {"role": "assistant", "content": "完整回答\n第二段"},
        "finish_reason": "stop",
    }
    assert result["model"] == "gemini-3.7-flash[防截断]"
    model, request, _ = upstream.call_args.args
    assert model == "gemini-3.7-flash"
    assert request.systemInstruction.parts[0].text == "用中文回答"
    assert request.tools[0].functionDeclarations[0]["name"] == "aistudio_full_reply"
    assert request.toolConfig == {
        "functionCallingConfig": {
            "mode": "ANY",
            "allowedFunctionNames": ["aistudio_full_reply"],
        }
    }


@pytest.mark.asyncio
async def test_streaming_alias_waits_for_complete_envelope_and_returns_usage(
    api, monkeypatch
):
    async def upstream():
        payload = full_reply()
        candidate = payload["candidates"][0]
        reason = candidate.pop("finishReason")
        event = "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
        encoded = event.encode()
        for index in range(0, len(encoded), 5):
            yield encoded[index : index + 5]
        yield "data: " + json.dumps({"candidates": [{"finishReason": reason}]}) + "\n\n"

    handle = AsyncMock(return_value=StreamingResponse(upstream()))
    monkeypatch.setattr(routes, "handle_gemini_generate_content", handle)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.7-flash[防截断]",
                "messages": [{"role": "user", "content": "继续故事"}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )
    events = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert (
        "".join(
            choice["delta"].get("content", "")
            for event in events
            for choice in event.get("choices", [])
        )
        == "完整回答\n第二段"
    )
    assert all(
        "tool_calls" not in choice["delta"]
        for event in events
        for choice in event.get("choices", [])
    )
    assert events[-2]["choices"][0]["finish_reason"] == "stop"
    assert events[-1]["usage"]["total_tokens"] == 10
    assert handle.call_args.args[0] == "gemini-3.7-flash"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_alias_rejects_missing_real_finish_in_generation_service(
    api, monkeypatch, stream
):
    client = MagicMock(spec=AIStudioClient)
    client.prepare_request = AsyncMock(
        return_value=PreparedRequest(url="https://example.test", headers={}, body="[]")
    )
    client.generate_content = AsyncMock(
        return_value=ModelOutput(
            candidates=[
                Candidate(
                    function_calls=[
                        {"name": "aistudio_full_reply", "args": {"content": "cut off"}}
                    ]
                )
            ]
        )
    )

    async def upstream(**kwargs):
        yield (
            "tool_calls",
            [{"name": "aistudio_full_reply", "args": {"content": "cut off"}}],
        )

    client.stream_generate_content = upstream
    app.dependency_overrides[get_client] = lambda: client
    monkeypatch.setattr(
        api_service_gemini, "ensure_active_account", AsyncMock(return_value=None)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api_client:
        response = await api_client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.7-flash[防截断]",
                "messages": [{"role": "user", "content": "story"}],
                "stream": stream,
            },
        )
    if stream:
        assert '"error"' in response.text
        assert '"finish_reason":"stop"' not in response.text
        assert '"content":"cut off"' not in response.text
    else:
        assert response.status_code == 502


@pytest.mark.asyncio
async def test_model_list_adds_only_supported_text_aliases_once(api, monkeypatch):
    from aistudio_api.infrastructure.gateway.model_discovery import model_discovery

    monkeypatch.setattr(
        model_discovery,
        "get_models",
        AsyncMock(
            return_value=[
                {
                    "id": "gemini-3.7-flash",
                    "capabilities": ["chat_model", "function_declarations"],
                },
                {"name": "models/gemini-3.7-flash"},
                {"id": "gemini-3.1-flash-image"},
                {"id": "gemma-4-31b-it"},
                {
                    "id": "gemini-decision-1",
                    "supportedGenerationMethods": ["createDecision"],
                },
                {
                    "id": "gemini-tts-model",
                    "capabilities": ["function_declarations", "speech_route"],
                },
                {"id": "gemini-new-model", "capabilities": ["chat_model"]},
            ]
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/models")
    ids = [item["id"] for item in response.json()["data"]]
    assert ids == [
        "gemini-3.7-flash",
        "gemini-3.7-flash[防截断]",
        "gemini-3.1-flash-image",
        "gemma-4-31b-it",
        "gemini-decision-1",
        "gemini-tts-model",
        "gemini-new-model",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "no_finish",
        "truncated_json",
        "max_tokens",
        "safety",
        "wrong_tool",
        "invalid_args",
        "multiple_tools",
        "plain_text",
        "non_string",
    ],
)
@pytest.mark.parametrize("stream", [False, True])
async def test_unfinished_or_invalid_alias_never_returns_success_or_caches(
    api, monkeypatch, failure, stream
):
    from aistudio_api.api.reply_cache import get_reply_cache

    cache = get_reply_cache()
    await cache.configure(1)
    await cache.save(
        "alias-test",
        {"model": "previous", "choices": [{"message": {"content": "previous good"}}]},
    )
    before = (await cache.status())["entries"]
    app.dependency_overrides[routes._reply_cache_owner] = lambda: "alias-test"
    payload = full_reply()
    candidate = payload["candidates"][0]
    if failure == "no_finish":
        candidate.pop("finishReason")
    elif failure == "max_tokens":
        candidate["finishReason"] = "MAX_TOKENS"
    elif failure == "safety":
        candidate["finishReason"] = "SAFETY"
    elif failure == "wrong_tool":
        candidate["content"]["parts"][1]["functionCall"]["name"] = "other"
    elif failure == "invalid_args":
        candidate["content"]["parts"][1]["functionCall"]["args"] = (
            '{"content":"unfinished'
        )
    elif failure == "multiple_tools":
        candidate["content"]["parts"].append(candidate["content"]["parts"][1])
    elif failure == "plain_text":
        candidate["content"]["parts"] = [{"text": "ordinary prose"}]
    elif failure == "non_string":
        candidate["content"]["parts"][1]["functionCall"]["args"] = {"content": 7}

    async def upstream():
        event = (
            ("data: " if stream else "")
            + json.dumps(payload, ensure_ascii=False)
            + "\n\n"
        )
        yield event[:-10] if failure == "truncated_json" else event

    monkeypatch.setattr(
        routes,
        "handle_gemini_generate_content",
        AsyncMock(
            return_value=StreamingResponse(upstream())
            if stream or failure == "truncated_json"
            else payload
        ),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gemini-3.7-flash[防截断]",
                    "messages": [{"role": "user", "content": "story"}],
                    "stream": stream,
                },
            )
        assert '"error"' in response.text
        assert '"finish_reason":"stop"' not in response.text
        assert '"content":"完整回答' not in response.text
        assert (await cache.status())["entries"] == before
    finally:
        app.dependency_overrides.pop(routes._reply_cache_owner, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["none", "missing_finish", "incomplete_outer", "truncated_argument"]
)
@pytest.mark.parametrize("selected_alias", [False, True])
async def test_real_gateway_preserves_completion_evidence_for_alias(
    api, monkeypatch, failure, selected_alias
):
    class UpstreamSession:
        active_context = None

        async def send_streaming_request(self, **kwargs):
            yield "status", 200
            part = [
                None,
                None,
                None,
                ["aistudio_full_reply", {"content": "wire complete"}],
            ]
            candidate: list[object] = [[[part], "model"]]
            if failure != "missing_finish":
                candidate.append(1)
            chunk = [[candidate], None, [3, 7, 10]]
            wire = ")]}'\n" + json.dumps([[chunk]])
            if failure == "incomplete_outer":
                wire = wire[:-1]
            elif failure == "truncated_argument":
                wire = wire[: wire.index("wire complete") + len("wire comp")]
            yield "chunk", wire.encode()

    gateway_client = AIStudioClient()
    gateway_client._streaming_gateway = StreamingGateway(
        session=cast(GatewaySession, UpstreamSession())
    )
    gateway_client.prepare_request = AsyncMock(
        return_value=PreparedRequest(url="https://example.test", headers={}, body="[]")
    )
    app.dependency_overrides[get_client] = lambda: gateway_client
    monkeypatch.setattr(
        api_service_gemini, "ensure_active_account", AsyncMock(return_value=None)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.7-flash[防截断]"
                if selected_alias
                else "gemini-3.7-flash",
                "messages": [{"role": "user", "content": "story"}],
                "stream": True,
            },
        )
    if not selected_alias:
        assert '"tool_calls"' in response.text
        assert '"finish_reason":"tool_calls"' in response.text
    elif failure == "none":
        assert '"content":"wire complete"' in response.text
        assert '"finish_reason":"stop"' in response.text
    else:
        assert '"error"' in response.text
        events = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        assert (
            next(event["error"]["code"] for event in events if "error" in event) == 502
        )
        assert '"content":"wire complete"' not in response.text
        assert '"finish_reason":"stop"' not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "conflict",
    [
        {"tools": [{"type": "function", "function": {"name": "own"}}]},
        {"tool_choice": "none"},
        {"function_call": "auto"},
        {"functions": [{"name": "legacy"}]},
        {"response_format": {"type": "json_object"}},
    ],
)
async def test_alias_rejects_conflicting_client_output_controls(
    api, monkeypatch, conflict
):
    upstream = AsyncMock(return_value=full_reply())
    monkeypatch.setattr(routes, "handle_gemini_generate_content", upstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.7-flash[防截断]",
                "messages": [{"role": "user", "content": "story"}],
                **conflict,
            },
        )
    assert response.status_code == 400
    upstream.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_model_preserves_client_function_calls(api, monkeypatch):
    upstream = AsyncMock(return_value=full_reply(name="own"))
    monkeypatch.setattr(routes, "handle_gemini_generate_content", upstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.7-flash",
                "messages": [{"role": "user", "content": "story"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "own", "parameters": {"type": "object"}},
                    }
                ],
            },
        )
    assert response.status_code == 200
    assert (
        response.json()["choices"][0]["message"]["tool_calls"][0]["function"]["name"]
        == "own"
    )
    assert response.json()["choices"][0]["finish_reason"] == "tool_calls"
    assert upstream.call_args.kwargs == {"stream": False}


@pytest.mark.asyncio
async def test_alias_keeps_internal_tool_with_default_tools_config(
    api, monkeypatch, tmp_path
):
    from aistudio_api.infrastructure.gateway.model_defaults import (
        invalidate_config_cache,
    )

    config = tmp_path / "defaults.yaml"
    config.write_text(
        "model_defaults:\n  profiles:\n    - name: text\n      match:\n        prefixes: [gemini-]\n      default_tools: [google_search, code_execution]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(config))
    invalidate_config_cache()
    client = MagicMock(spec=AIStudioClient)
    client.prepare_request = AsyncMock(
        return_value=PreparedRequest(url="https://example.test", headers={}, body="[]")
    )
    client.generate_content = AsyncMock(
        return_value=ModelOutput(
            candidates=[
                Candidate(
                    finish_reason=1,
                    function_calls=[
                        {"name": "aistudio_full_reply", "args": {"content": "complete"}}
                    ],
                )
            ]
        )
    )
    app.dependency_overrides[get_client] = lambda: client
    monkeypatch.setattr(
        api_service_gemini, "ensure_active_account", AsyncMock(return_value=None)
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as api_client:
            response = await api_client.post(
                "/v1/chat/completions",
                json={
                    "model": "gemini-3.7-flash[防截断]",
                    "messages": [{"role": "user", "content": "story"}],
                },
            )
        assert response.status_code == 200
        tools = client.prepare_request.call_args.kwargs["tools"]
        assert len(tools) == 1
        assert tools[0][1][0][0] == "aistudio_full_reply"
        assert client.prepare_request.call_args.kwargs["tool_config"][1] == [
            2,
            ["aistudio_full_reply"],
        ]
    finally:
        invalidate_config_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_complete_alias_reply_can_be_replayed_as_plain_prose(
    api, monkeypatch, stream
):
    from aistudio_api.api.reply_cache import get_reply_cache

    cache = get_reply_cache()
    await cache.configure(1)
    app.dependency_overrides[routes._reply_cache_owner] = lambda: "alias-owner"

    async def upstream():
        yield "data: " + json.dumps(full_reply(), ensure_ascii=False) + "\n\n"

    handle = AsyncMock(
        return_value=StreamingResponse(upstream()) if stream else full_reply()
    )
    monkeypatch.setattr(routes, "handle_gemini_generate_content", handle)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gemini-3.7-flash[防截断]",
                    "messages": [{"role": "user", "content": "story"}],
                    "stream": stream,
                },
            )
            assert first.status_code == 200
            entry = (await cache.status())["entries"][0]
            await cache.arm(entry["id"])
            replay = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gemini-3.7-flash[防截断]",
                    "messages": [{"role": "user", "content": "story"}],
                },
            )
        assert replay.headers["X-Aistudio-Reply-Cache"] == "replay"
        assert replay.json()["model"] == "gemini-3.7-flash[防截断]"
        assert replay.json()["choices"][0]["message"] == {
            "role": "assistant",
            "content": "完整回答\n第二段",
        }
        handle.assert_awaited_once()
    finally:
        app.dependency_overrides.pop(routes._reply_cache_owner, None)


@pytest.mark.asyncio
async def test_alias_idle_keepalive_and_cancellation_close_upstream(monkeypatch):
    from aistudio_api.api.schemas import OpenAIChatCompletionRequest

    blocked = asyncio.Event()
    closed = asyncio.Event()

    async def upstream():
        try:
            await blocked.wait()
            yield "unreachable"
        finally:
            closed.set()

    monkeypatch.setattr(routes, "_KEEPALIVE_INTERVAL", 0.01)
    monkeypatch.setattr(
        routes,
        "handle_gemini_generate_content",
        AsyncMock(return_value=StreamingResponse(upstream())),
    )
    response = await routes.create_chat_completion(
        OpenAIChatCompletionRequest(
            model="gemini-3.7-flash[防截断]",
            messages=[{"role": "user", "content": "story"}],
            stream=True,
        ),
        MagicMock(spec=AIStudioClient),
    )
    assert isinstance(response, StreamingResponse)
    iterator = response.body_iterator
    assert isinstance(iterator, AsyncGenerator)
    first = await anext(iterator)
    assert isinstance(first, str) and '"role":"assistant"' in first
    assert await anext(iterator) == ": keepalive\n\n"
    await iterator.aclose()
    assert closed.is_set()
