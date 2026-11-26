"""OpenAI-facing behavior of the opt-in full-reply tool."""

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from aistudio_api.api import routes_openai as routes
from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.schemas import (
    GeminiGenerateContentRequest,
    GeminiTool,
    OpenAIChatCompletionRequest,
)
from aistudio_api.application import api_service_gemini
from aistudio_api.application.anti_truncation import (
    FullReplyToolPolicy,
    tool_policy,
    unwrap_response,
    unwrap_stream,
    validate_selection,
    wrap_request,
)
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


def _client_tool_request(
    choice=None, *, names=("lookup", "calculate"), messages=None, **kwargs
):
    return OpenAIChatCompletionRequest(
        model="gemini-4a[防截断]",
        messages=messages or [{"role": "user", "content": "synthetic tool request"}],
        tools=[
            {
                "type": "function",
                "function": {"name": name, "parameters": {"type": "object"}},
            }
            for name in names
        ],
        tool_choice=choice,
        **kwargs,
    )


def _function_names(request: GeminiGenerateContentRequest) -> list[object]:
    assert request.tools is not None
    names: list[object] = []
    for tool in request.tools:
        assert tool.functionDeclarations is not None
        names.extend(declaration["name"] for declaration in tool.functionDeclarations)
    return names


@pytest.mark.parametrize(
    "choice,allowed,allow_final",
    [
        (None, ("lookup", "calculate"), True),
        ("auto", ("lookup", "calculate"), True),
        ("none", (), True),
        ("required", ("lookup", "calculate"), False),
        (
            {"type": "function", "function": {"name": "calculate"}},
            ("calculate",),
            False,
        ),
    ],
)
def test_full_reply_tool_policy_preserves_choice_semantics(
    choice, allowed, allow_final
):
    request = _client_tool_request(choice)
    validate_selection(request)
    policy = tool_policy(request)
    assert policy == FullReplyToolPolicy(("lookup", "calculate"), allowed, allow_final)
    assert request.tools is not None
    gemini = GeminiGenerateContentRequest(
        contents=[],
        tools=[
            GeminiTool(functionDeclarations=[cast(dict[str, object], tool["function"])])
            for tool in request.tools
        ],
    )
    original = gemini.model_dump()
    wrapped = wrap_request(gemini, policy)
    assert gemini.model_dump() == original
    assert _function_names(wrapped) == [
        "lookup",
        "calculate",
        "aistudio_full_reply",
    ]
    assert wrapped.toolConfig == {
        "functionCallingConfig": {
            "mode": "ANY",
            "allowedFunctionNames": [
                *allowed,
                *(["aistudio_full_reply"] if allow_final else []),
            ],
        }
    }


@pytest.mark.parametrize("choice", [None, "none", "auto"])
def test_full_reply_policy_without_client_tools_allows_only_envelope(choice):
    request = _client_tool_request(choice, names=())
    policy = tool_policy(request)
    assert policy == FullReplyToolPolicy()
    wrapped = wrap_request(GeminiGenerateContentRequest(contents=[]), policy)
    assert wrapped.toolConfig == {
        "functionCallingConfig": {
            "mode": "ANY",
            "allowedFunctionNames": ["aistudio_full_reply"],
        }
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"names": ("same", "same")},
        {"names": ("",)},
        {"names": (" spaced ",)},
        {"choice": "required", "names": ()},
        {"choice": "unsupported"},
        {"choice": {"type": "function", "function": {"name": "unknown"}}},
        {"parallel_tool_calls": True},
        {"messages": [{"role": "assistant", "function_call": {"name": "lookup"}}]},
        {"messages": [{"role": "function", "name": "lookup", "content": "result"}]},
        {
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"function": {"name": "lookup"}},
                        {"function": {"name": "calculate"}},
                    ],
                }
            ]
        },
    ],
)
def test_full_reply_policy_rejects_conflicting_or_ambiguous_tools(kwargs):
    with pytest.raises(HTTPException) as error:
        validate_selection(_client_tool_request(**kwargs))
    assert error.value.status_code == 400


def test_full_reply_policy_keeps_history_functions_independent_of_current_tools():
    request = _client_tool_request(
        messages=[
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "prior_function"}, "id": "prior-call"}
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "prior-call",
                "content": "synthetic prior result",
            },
        ]
    )
    assert tool_policy(request).client_names == ("lookup", "calculate")


def test_full_reply_envelope_name_avoids_client_declarations_and_history():
    request = _client_tool_request(
        names=("aistudio_full_reply", "aistudio_full_reply_1"),
        messages=[
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "aistudio_full_reply_2"}, "id": "prior-call"}
                ],
            },
            {"role": "tool", "name": "aistudio_full_reply_3", "content": "synthetic"},
        ],
    )
    policy = tool_policy(request)
    assert policy.envelope_name == "aistudio_full_reply_4"
    assert request.tools is not None
    gemini = GeminiGenerateContentRequest(
        contents=[],
        tools=[
            GeminiTool(functionDeclarations=[cast(dict[str, object], tool["function"])])
            for tool in request.tools
        ],
    )
    wrapped = wrap_request(gemini, policy)
    assert _function_names(wrapped) == [
        "aistudio_full_reply",
        "aistudio_full_reply_1",
        "aistudio_full_reply_4",
    ]
    assert wrapped.systemInstruction is not None
    instruction = wrapped.systemInstruction.parts[-1].text
    assert isinstance(instruction, str)
    assert "call aistudio_full_reply_4 exactly once" in instruction
    external = full_reply(name="aistudio_full_reply", args={"synthetic": True})
    assert unwrap_response(external, policy)["aistudio_full_reply"] == {
        "mode": "client_tool_call"
    }
    final = full_reply(
        name="aistudio_full_reply_4", args={"content": "complete synthetic answer"}
    )
    final["candidates"][0]["content"]["parts"].insert(
        1, {"text": "调用函数 `aistudio_full_reply_4` 输出如下内容。"}
    )
    result = unwrap_response(final, policy)
    assert result["aistudio_full_reply"] == {"mode": "function_envelope"}
    candidates = result["candidates"]
    assert isinstance(candidates, list)
    assert candidates[0]["content"]["parts"] == [{"text": "complete synthetic answer"}]


@pytest.mark.parametrize(
    "choice",
    [None, "auto", "required", {"type": "function", "function": {"name": "lookup"}}],
)
def test_full_reply_unwrap_keeps_complete_allowed_client_call_and_signature(choice):
    payload = full_reply(
        name="lookup",
        args={"query": "synthetic", "nested": {"values": [1, True, None]}},
    )
    call_part = payload["candidates"][0]["content"]["parts"][1]
    call_part["thoughtSignature"] = "synthetic-opaque-signature"
    call_part["functionCall"]["id"] = "synthetic-call-id"
    result = unwrap_response(payload, tool_policy(_client_tool_request(choice)))
    assert result["aistudio_full_reply"] == {"mode": "client_tool_call"}
    assert result["candidates"] == payload["candidates"]
    candidates = result["candidates"]
    assert isinstance(candidates, list)
    assert candidates[0]["content"]["parts"][1] == call_part


@pytest.mark.parametrize(
    "case",
    [
        "unknown",
        "none",
        "wrong_named",
        "mixed",
        "multiple",
        "missing_args",
        "string_args",
        "list_args",
        "nonfinite_args",
        "invalid_id",
        "empty_id",
        "invalid_signature",
        "nonstop",
        "missing_finish",
        "thought_call",
    ],
)
def test_full_reply_unwrap_rejects_invalid_client_tool_turn(case):
    payload = full_reply(name="lookup", args={"query": "synthetic"})
    policy = tool_policy(_client_tool_request())
    candidate = payload["candidates"][0]
    part = candidate["content"]["parts"][1]
    if case == "unknown":
        part["functionCall"]["name"] = "unknown"
    elif case == "none":
        policy = tool_policy(_client_tool_request("none"))
    elif case == "wrong_named":
        policy = tool_policy(
            _client_tool_request(
                {"type": "function", "function": {"name": "calculate"}}
            )
        )
    elif case in ("mixed", "multiple"):
        candidate["content"]["parts"].append(
            {
                "functionCall": {
                    "name": "aistudio_full_reply" if case == "mixed" else "calculate",
                    "args": {"content": "synthetic"},
                }
            }
        )
    elif case == "missing_args":
        part["functionCall"].pop("args")
    elif case in ("string_args", "list_args", "nonfinite_args"):
        part["functionCall"]["args"] = {
            "string_args": '{"query":"unfinished',
            "list_args": [],
            "nonfinite_args": {"value": float("nan")},
        }[case]
    elif case in ("invalid_id", "empty_id"):
        part["functionCall"]["id"] = 12 if case == "invalid_id" else ""
    elif case == "invalid_signature":
        part["thoughtSignature"] = 12
    elif case == "nonstop":
        candidate["finishReason"] = "MAX_TOKENS"
    elif case == "missing_finish":
        candidate.pop("finishReason")
    elif case == "thought_call":
        part["thought"] = True
    with pytest.raises(HTTPException) as error:
        unwrap_response(payload, policy)
    assert error.value.status_code == 502


@pytest.mark.parametrize(
    "choice", ["required", {"type": "function", "function": {"name": "lookup"}}]
)
@pytest.mark.parametrize("plain", [False, True])
def test_full_reply_required_choice_cannot_be_satisfied_by_final_answer(choice, plain):
    payload = full_reply()
    if plain:
        payload["candidates"][0]["content"]["parts"] = [
            {"text": "synthetic final answer"}
        ]
    with pytest.raises(HTTPException, match="requires a client function call"):
        unwrap_response(payload, tool_policy(_client_tool_request(choice)))


@pytest.mark.asyncio
async def test_full_reply_buffered_stream_preserves_client_call_after_complete_validation():
    payload = full_reply(name="lookup", args={"query": "synthetic"})

    async def chunks():
        encoded = ("data: " + json.dumps(payload) + "\n\n").encode()
        for offset in range(0, len(encoded), 7):
            yield encoded[offset : offset + 7]

    events = [
        event
        async for event in unwrap_stream(chunks(), tool_policy(_client_tool_request()))
    ]
    assert len(events) == 1
    result = json.loads(events[0][6:])
    assert result["aistudio_full_reply"] == {"mode": "client_tool_call"}
    assert result["candidates"] == payload["candidates"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_failure", ["short_reply", "missing_finish"])
async def test_streaming_alias_returns_complete_generation_when_upstream_stream_cuts_off(
    api, monkeypatch, stream_failure
):
    complete = "第一段\n完整的最后一段。"

    async def handle(model, request, client, *, stream, **kwargs):
        if not stream:
            return full_reply(args={"content": complete})

        async def cut_off():
            payload = full_reply(args={"content": "第一段"})
            if stream_failure == "missing_finish":
                payload["candidates"][0].pop("finishReason")
            yield "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

        return StreamingResponse(cut_off())

    upstream = AsyncMock(side_effect=handle)
    monkeypatch.setattr(routes, "handle_gemini_generate_content", upstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-4a[防截断]",
                "messages": [{"role": "user", "content": "返回完整的两段文字"}],
                "stream": True,
                "max_tokens": 4096,
                "stream_options": {"include_usage": True},
            },
        )
    assert response.headers["content-type"].startswith("text/event-stream")
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
        == complete
    )
    assert events[-2]["choices"][0]["finish_reason"] == "stop"
    assert events[-1]["usage"]["total_tokens"] == 10
    assert response.text.count("data: [DONE]") == 1
    assert all("error" not in event for event in events)
    assert upstream.call_args.args[0] == "gemini-4a"
    assert upstream.call_args.kwargs == {"stream": False, "require_finish_reason": True}
    assert upstream.call_args.args[1].generationConfig.maxOutputTokens == 4096


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
        event = json.dumps(payload, ensure_ascii=False)
        encoded = event.encode()
        yield b"\n"
        for index in range(0, len(encoded), 5):
            yield encoded[index : index + 5]

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
    assert handle.call_args.kwargs == {"stream": False, "require_finish_reason": True}


def _alias_prose(response, *, stream):
    if not stream:
        payload = response.json()
        return payload["choices"][0]["message"]["content"], [
            payload["aistudio_full_reply"]
        ]
    events = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    content_events = [
        event
        for event in events
        if any(
            choice.get("delta", {}).get("content")
            for choice in event.get("choices", [])
        )
    ]
    text = "".join(
        choice["delta"].get("content", "")
        for event in content_events
        for choice in event["choices"]
    )
    assert response.text.count("data: [DONE]") == 1
    return text, [event["aistudio_full_reply"] for event in content_events]


def _alias_upstream(payload):
    async def handle(*args, stream, **kwargs):
        if not stream:
            return payload

        async def events():
            yield "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

        return StreamingResponse(events())

    return AsyncMock(side_effect=handle)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "case",
    [
        "envelope",
        "mechanical_preface",
        "before_and_after",
        "shared_part",
        "thoughts",
        "plain_text",
        "plain_parts",
        "custom_plain_text",
    ],
)
async def test_alias_preserves_visible_parts_and_reports_mode(
    api, monkeypatch, case, stream
):
    answer = "  完整函数回答\n第二段  "
    call = {
        "functionCall": {"name": "aistudio_full_reply", "args": {"content": answer}}
    }
    preface = "调用函数 `aistudio_full_reply` 输出如下内容。"
    cases = {
        "envelope": ([call], answer),
        "mechanical_preface": ([{"text": " \n" + preface + "\t"}, call], answer),
        "before_and_after": (
            [{"text": "Opening\n"}, call, {"text": "\nEnding"}],
            "Opening\n" + answer + "\nEnding",
        ),
        "shared_part": ([{"text": "Opening\n", **call}], "Opening\n" + answer),
        "thoughts": (
            [
                {"text": "private", "thought": True},
                call,
                {"text": "hidden", "thought": True},
            ],
            answer,
        ),
        "plain_text": ([{"text": "  普通正文\n第二段  "}], "  普通正文\n第二段  "),
        "plain_parts": (
            [{"text": "  第一段"}, {"text": "\n"}, {"text": "第二段  "}],
            "  第一段\n第二段  ",
        ),
        "custom_plain_text": (
            [{"text": "思考说明\n<!-- charThink: synthetic -->\n状态输出"}],
            "思考说明\n<!-- charThink: synthetic -->\n状态输出",
        ),
    }
    parts, expected = cases[case]
    payload = full_reply()
    payload["candidates"][0]["content"]["parts"] = parts

    monkeypatch.setattr(
        routes,
        "handle_gemini_generate_content",
        _alias_upstream(payload),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-4-argon[防截断]",
                "messages": [{"role": "user", "content": "story"}],
                "stream": stream,
            },
        )
    assert response.status_code == 200
    text, metadata = _alias_prose(response, stream=stream)
    assert text == expected
    mode = (
        "plain_text_fallback"
        if case in ("plain_text", "plain_parts", "custom_plain_text")
        else "function_envelope"
    )
    assert metadata and all(item == {"mode": mode} for item in metadata)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mode", ["function_envelope", "plain_text_fallback"])
async def test_alias_mode_survives_cache_and_replay(api, monkeypatch, stream, mode):
    from aistudio_api.api.reply_cache import get_reply_cache

    payload = full_reply()
    if mode == "plain_text_fallback":
        payload["candidates"][0]["content"]["parts"] = [{"text": "完整回答\n第二段"}]
    cache = get_reply_cache()
    await cache.configure(1)
    app.dependency_overrides[routes._reply_cache_owner] = lambda: "mode-owner"

    handler = _alias_upstream(payload)
    monkeypatch.setattr(routes, "handle_gemini_generate_content", handler)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            request = {
                "model": "gemini-4-argon[防截断]",
                "messages": [{"role": "user", "content": "story"}],
            }
            first = await client.post(
                "/v1/chat/completions", json={**request, "stream": stream}
            )
            assert first.status_code == 200
            assert _alias_prose(first, stream=stream) == (
                "完整回答\n第二段",
                [{"mode": mode}],
            )
            entry = (await cache.status())["entries"][0]
            await cache.arm(entry["id"])
            replay = await client.post(
                "/v1/chat/completions", json={**request, "stream": not stream}
            )
        assert replay.headers["X-Aistudio-Reply-Cache"] == "replay"
        assert _alias_prose(replay, stream=not stream) == (
            "完整回答\n第二段",
            [{"mode": mode}],
        )
        handler.assert_awaited_once()
    finally:
        app.dependency_overrides.pop(routes._reply_cache_owner, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "failure",
    [
        "wrong_tool",
        "invalid_args",
        "multiple_tools",
        "malformed_call",
        "thought_call",
        "inline_data",
        "file_data",
        "file_data_with_call",
        "executable_code",
        "code_execution_result",
        "invalid_part",
    ],
)
async def test_alias_rejects_invalid_envelopes_instead_of_plain_text_fallback(
    api, monkeypatch, stream, failure
):
    function: dict[str, object] = {
        "name": "aistudio_full_reply",
        "args": {"content": "complete"},
    }
    call: dict[str, object] = {"functionCall": function}
    parts = [{"text": "Visible text must not bypass the invalid envelope."}, call]
    if failure == "wrong_tool":
        function["name"] = "other"
    elif failure == "invalid_args":
        function["args"] = '{"content":"unfinished'
    elif failure == "multiple_tools":
        parts.append(call)
    elif failure == "malformed_call":
        call["functionCall"] = "invalid"
    elif failure == "thought_call":
        call["thought"] = True
    elif failure == "inline_data":
        parts = [{"text": "Visible text"}, {"inlineData": {}}]
    elif failure in ("file_data", "file_data_with_call"):
        parts = [
            {"text": "Visible text"},
            {"fileData": {"fileUri": "synthetic://file"}},
        ]
        if failure == "file_data_with_call":
            parts.append(call)
    elif failure == "executable_code":
        parts.append({"executableCode": {"language": "PYTHON", "code": "pass"}})
    elif failure == "code_execution_result":
        parts = [
            {"text": "Visible text"},
            {"codeExecutionResult": {"outcome": "OUTCOME_OK", "output": "synthetic"}},
        ]
    elif failure == "invalid_part":
        parts.append("invalid")
    payload = full_reply()
    payload["candidates"][0]["content"]["parts"] = parts
    monkeypatch.setattr(
        routes, "handle_gemini_generate_content", _alias_upstream(payload)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-4-argon[防截断]",
                "messages": [{"role": "user", "content": "story"}],
                "stream": stream,
            },
        )
    assert '"error"' in response.text
    assert '"aistudio_full_reply"' not in response.text
    if stream:
        assert '"finish_reason":"stop"' not in response.text
    else:
        assert '"choices"' not in response.text


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
@pytest.mark.parametrize(
    "case",
    [
        "empty_text",
        "whitespace",
        "unicode_whitespace",
        "outside_text",
        "inline_data",
        "max_tokens",
        "prohibited_content",
        "multiple_tools",
        "missing_tool",
        "invalid_args",
    ],
)
async def test_real_gateway_alias_accepts_visible_text_and_preserves_guards(
    api, monkeypatch, case
):
    answer = "  第一段\r\n第二段\t "
    extra_text = {
        "empty_text": "",
        "unicode_whitespace": "\u3000\r\n",
        "outside_text": " extra answer ",
    }.get(case, " \r\n\t")
    args = '{"content":"unfinished' if case == "invalid_args" else {"content": answer}
    call_part = [None, None, None, ["aistudio_full_reply", args]]
    parts = [[True, "private reasoning"], [None, extra_text], call_part]
    if case == "multiple_tools":
        parts.append(call_part)
    elif case == "missing_tool":
        parts.pop()
    elif case == "inline_data":
        parts.append([None, None, ["audio/pcm", "AAA="]])
    finish = {"max_tokens": 2, "prohibited_content": 8}.get(case, 1)
    candidate = [[parts, "model"], finish]
    wire = ")]}'\n" + json.dumps([[[[candidate], None, [3, 7, 10]]]])

    class UpstreamSession:
        active_context = None

        async def send_request(self, **kwargs):
            return 200, wire.encode()

    # Skip session construction so the real client parses only synthetic wire data.
    gateway_client = AIStudioClient.__new__(AIStudioClient)
    gateway_client._session = cast(GatewaySession, UpstreamSession())
    prepared = PreparedRequest(url="https://example.test", headers={}, body="[]")
    gateway_client.prepare_request = AsyncMock(return_value=prepared)
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
                "model": "gemini-4-argon[防截断]",
                "messages": [{"role": "user", "content": "story"}],
                "stream": False,
            },
        )
    assert prepared.require_finish_reason is True
    if case in ("empty_text", "whitespace", "unicode_whitespace", "outside_text"):
        assert response.status_code == 200
        assert response.json()["choices"][0] == {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": extra_text + answer if case == "outside_text" else answer,
            },
            "finish_reason": "stop",
        }
    else:
        assert response.status_code == 502
        assert response.json()["error"]["type"] == "upstream_response_error"
        assert '"choices"' not in response.text


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
        "prohibited_content",
        "inline_data",
        "wrong_tool",
        "invalid_args",
        "multiple_tools",
        "empty_text",
        "thought_only",
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
    elif failure == "prohibited_content":
        candidate["finishReason"] = "PROHIBITED_CONTENT"
    elif failure == "inline_data":
        candidate["content"]["parts"].append(
            {"inlineData": {"mimeType": "image/png", "data": "AA=="}}
        )
    elif failure == "wrong_tool":
        candidate["content"]["parts"][1]["functionCall"]["name"] = "other"
    elif failure == "invalid_args":
        candidate["content"]["parts"][1]["functionCall"]["args"] = (
            '{"content":"unfinished'
        )
    elif failure == "multiple_tools":
        candidate["content"]["parts"].append(candidate["content"]["parts"][1])
    elif failure == "empty_text":
        candidate["content"]["parts"] = [{"text": " \n\t"}]
    elif failure == "thought_only":
        candidate["content"]["parts"] = [{"text": "private", "thought": True}]
    elif failure == "non_string":
        candidate["content"]["parts"][1]["functionCall"]["args"] = {"content": 7}

    async def upstream():
        event = json.dumps(payload, ensure_ascii=False) + "\n\n"
        yield event[:-10] if failure == "truncated_json" else event

    monkeypatch.setattr(
        routes,
        "handle_gemini_generate_content",
        AsyncMock(
            return_value=StreamingResponse(upstream())
            if failure == "truncated_json"
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

        def wire(self):
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
            return wire.encode()

        async def send_request(self, **kwargs):
            return 200, self.wire()

        async def send_streaming_request(self, **kwargs):
            yield "status", 200
            yield "chunk", self.wire()

    gateway_client = AIStudioClient()
    gateway_client._session = cast(GatewaySession, UpstreamSession())
    gateway_client._streaming_gateway = StreamingGateway(session=gateway_client.session)
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
    if not selected_alias and failure != "truncated_argument":
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
        assert '"finish_reason":"tool_calls"' not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "conflict",
    [
        {
            "tools": [
                {"type": "function", "function": {"name": "same"}},
                {"type": "function", "function": {"name": "same"}},
            ]
        },
        {"tool_choice": "required"},
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

    handle = AsyncMock(return_value=full_reply())
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
        assert handle.call_args.kwargs == {
            "stream": False,
            "require_finish_reason": True,
        }
    finally:
        app.dependency_overrides.pop(routes._reply_cache_owner, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [False, True])
async def test_alias_idle_keepalive_closes_upstream_on_finish_or_cancel(
    monkeypatch, complete
):
    from aistudio_api.api.schemas import OpenAIChatCompletionRequest

    blocked = asyncio.Event()
    closed = asyncio.Event()

    async def upstream():
        try:
            await blocked.wait()
            yield json.dumps(full_reply(), ensure_ascii=False)
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
    assert await anext(iterator) == ": keepalive\n\n"
    assert await anext(iterator) == ": keepalive\n\n"
    if complete:
        blocked.set()
        chunks = [chunk async for chunk in iterator]
        body = "".join(str(chunk) for chunk in chunks)
        assert '"content":"完整回答\\n第二段"' in body
        assert '"finish_reason":"stop"' in body
        assert body.count("data: [DONE]") == 1
    await iterator.aclose()
    assert closed.is_set()


@pytest.mark.asyncio
async def test_streaming_alias_preserves_buffered_upstream_error(api, monkeypatch):
    async def upstream():
        yield b"\n"
        yield json.dumps({"error": {"code": 429, "message": "Try later"}})

    handle = AsyncMock(return_value=StreamingResponse(upstream()))
    monkeypatch.setattr(routes, "handle_gemini_generate_content", handle)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-4a[防截断]",
                "messages": [{"role": "user", "content": "test"}],
                "stream": True,
            },
        )
    assert '"code":429' in response.text
    assert "Try later" in response.text
    assert '"finish_reason":"stop"' not in response.text
    assert response.text.count("data: [DONE]") == 1
