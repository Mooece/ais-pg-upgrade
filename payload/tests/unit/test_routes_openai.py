import base64
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.responses import StreamingResponse

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.response_models import (
    GeminiCandidateResponse,
    GeminiContentResponse,
    GeminiFunctionCallPayload,
    GeminiGenerateContentResponse,
    GeminiInlineDataResponse,
    GeminiPartResponse,
    GeminiUsageMetadata,
)
from aistudio_api.api.routes_openai import (
    gemini_response_to_openai,
    openai_request_to_gemini,
)
from aistudio_api.api.schemas import OpenAIChatCompletionRequest
from aistudio_api.config import settings


def _build_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def test_openai_request_maps_system_multimodal_and_sampling_options():
    request = OpenAIChatCompletionRequest(
        model="gemini-3.7-flash",
        messages=[
            {"role": "system", "content": "Be concise"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + base64.b64encode(b"png").decode()
                        },
                    },
                ],
            },
        ],
        temperature=0.2,
        top_p=0.8,
        max_completion_tokens=120,
    )

    converted = openai_request_to_gemini(request)
    assert converted.systemInstruction is not None
    assert converted.systemInstruction.parts[0].text == "Be concise"
    assert converted.contents[0].parts[0].text == "Describe this"
    assert converted.contents[0].parts[1].inlineData is not None
    assert (
        converted.contents[0].parts[1].inlineData.data
        == base64.b64encode(b"png").decode()
    )
    assert converted.generationConfig is not None
    assert converted.generationConfig.maxOutputTokens == 120


def test_openai_function_call_history_preserves_ids_and_signature():
    from aistudio_api.api import routes_openai

    routes_openai._remember_tool_signature("call_weather", "sig-123")
    request = OpenAIChatCompletionRequest(
        model="gemini-3.7-flash",
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_weather",
                        "type": "function",
                        "function": {
                            "name": "weather",
                            "arguments": '{"city":"Shanghai"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_weather",
                "content": "Sunny",
            },
        ],
        tools=[
            {
                "type": "function",
                "function": {"name": "weather", "parameters": {"type": "object"}},
            }
        ],
    )

    converted = openai_request_to_gemini(request)
    call = converted.contents[0].parts[0]
    assert call.functionCall is not None
    assert call.functionCall.args == {"city": "Shanghai"}
    assert call.functionCall.id == "call_weather"
    assert call.thoughtSignature == "sig-123"
    assert converted.contents[1].parts[0].functionResponse is not None
    assert converted.contents[1].parts[0].functionResponse.name == "weather"


def test_openai_function_call_id_carries_signature_across_process_boundary():
    request = OpenAIChatCompletionRequest(
        model="gemini-3.7-flash",
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_weather--sig-c2lnLTEyMw",
                        "type": "function",
                        "function": {
                            "name": "weather",
                            "arguments": '{"city":"Shanghai"}',
                        },
                    }
                ],
            }
        ],
    )
    converted = openai_request_to_gemini(request)
    call = converted.contents[0].parts[0]
    assert call.functionCall is not None
    assert call.functionCall.id == "call_weather"
    assert call.thoughtSignature == "sig-123"


@pytest.mark.parametrize(
    "field", ["signature", "thought_signature", "thoughtSignature"]
)
@pytest.mark.parametrize("location", ["tool_call", "function"])
def test_openai_function_call_preserves_explicit_signature(field, location):
    function = {"name": "context", "arguments": "{}"}
    tool_call = {
        "id": "default_api:context",
        "type": "function",
        "function": function,
    }
    target = tool_call if location == "tool_call" else function
    target[field] = "sig-from-client"
    request = OpenAIChatCompletionRequest(
        model="gemini-4-argon",
        messages=[
            {"role": "assistant", "content": None, "tool_calls": [tool_call]},
            {
                "role": "tool",
                "tool_call_id": "default_api:context",
                "content": "context result",
            },
        ],
    )

    converted = openai_request_to_gemini(request)

    call = converted.contents[0].parts[0]
    assert call.functionCall is not None
    assert call.functionCall.id == "default_api:context"
    assert call.thoughtSignature == "sig-from-client"
    result = converted.contents[1].parts[0].functionResponse
    assert result is not None
    assert result.id == "default_api:context"
    assert result.name == "context"


def test_explicit_tool_signature_overrides_encoded_id_and_stays_per_call():
    request = OpenAIChatCompletionRequest(
        model="gemini-4-argon",
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_context--sig-c2lnLTEyMw",
                        "type": "function",
                        "signature": "sig-context",
                        "function": {"name": "context", "arguments": "{}"},
                    },
                    {
                        "id": "call_reply--sig-c2lnLTEyMw",
                        "type": "function",
                        "signature": "",
                        "function": {"name": "reply", "arguments": "{}"},
                    },
                ],
            }
        ],
    )

    parts = openai_request_to_gemini(request).contents[0].parts

    assert parts[0].thoughtSignature == "sig-context"
    assert parts[0].functionCall is not None
    assert parts[0].functionCall.id == "call_context"
    assert parts[1].thoughtSignature == "sig-123"
    assert parts[1].functionCall is not None
    assert parts[1].functionCall.id == "call_reply"


def test_openai_context_tool_signature_from_google_extra_content():
    """SillyTavern's context binding uses extra_content.google.thoughtSignature."""
    from aistudio_api.application.chat_service import normalize_gemini_request
    from aistudio_api.infrastructure.gateway.wire_spec import PartIndex

    request = OpenAIChatCompletionRequest(
        model="gemini-4-argon",
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "default_api:context",
                        "type": "function",
                        "extra_content": {
                            "google": {
                                "thoughtSignature": "skip_thought_signature_validator"
                            }
                        },
                        "function": {
                            "name": "context",
                            "arguments": '{"query":"recent messages"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "default_api:context",
                "content": "context result",
            },
        ],
    )

    converted = openai_request_to_gemini(request)
    call = converted.contents[0].parts[0]

    assert call.functionCall is not None
    assert call.functionCall.name == "context"
    assert call.functionCall.args == {"query": "recent messages"}
    assert call.thoughtSignature == "skip_thought_signature_validator"

    normalized = normalize_gemini_request(converted, "gemini-4-argon")
    wire_part = normalized.contents[0].parts[0].to_wire()
    wire_call = wire_part[PartIndex.FUNCTION_CALL_ALT]
    assert isinstance(wire_call, list)
    assert wire_call[0] == "context"
    assert wire_call[2] == "default_api:context"
    assert (
        wire_part[PartIndex.THOUGHT_SIGNATURE]
        == "c2tpcF90aG91Z2h0X3NpZ25hdHVyZV92YWxpZGF0b3I="
    )


def test_openai_context_tool_call_without_signature_uses_google_fallback():
    """Synthetic OpenAI tool calls need Google's fallback marker when unsigned."""
    from aistudio_api.application.chat_service import normalize_gemini_request
    from aistudio_api.infrastructure.gateway.wire_spec import PartIndex

    request = OpenAIChatCompletionRequest(
        model="gemini-4-argon",
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "default_api:context",
                        "type": "function",
                        "function": {
                            "name": "context",
                            "arguments": '{"query":"sync"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "default_api:context",
                "content": "context result",
            },
        ],
    )

    converted = openai_request_to_gemini(request)
    call = converted.contents[0].parts[0]
    assert call.functionCall is not None
    assert call.functionCall.id == "default_api:context"
    assert call.thoughtSignature == "skip_thought_signature_validator"

    normalized = normalize_gemini_request(converted, "gemini-4-argon")
    wire_part = normalized.contents[0].parts[0].to_wire()
    wire_call = wire_part[PartIndex.FUNCTION_CALL_ALT]
    assert isinstance(wire_call, list)
    assert wire_call[0] == "context"
    assert wire_call[2] == "default_api:context"
    assert (
        wire_part[PartIndex.THOUGHT_SIGNATURE]
        == "c2tpcF90aG91Z2h0X3NpZ25hdHVyZV92YWxpZGF0b3I="
    )


def test_openai_request_rejects_remote_image_url_and_unknown_options():
    from fastapi import HTTPException

    request = OpenAIChatCompletionRequest(
        model="gemini-3.7-flash",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/a.png"},
                    }
                ],
            }
        ],
    )
    with pytest.raises(HTTPException, match="base64 data URLs"):
        openai_request_to_gemini(request)

    with pytest.raises(ValueError):
        OpenAIChatCompletionRequest.model_validate(
            {
                "model": "gemini-3.7-flash",
                "messages": [{"role": "user", "content": "hi"}],
                "unsupported_parameter": True,
            }
        )


def test_openai_request_accepts_common_client_metadata_and_default_options():
    request = OpenAIChatCompletionRequest.model_validate(
        {
            "model": "gemini-3.7-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "user": "client-user",
            "metadata": {"source": "desktop-client"},
            "parallel_tool_calls": False,
            "logprobs": False,
            "top_logprobs": 0,
            "logit_bias": {},
            "modalities": ["text"],
        }
    )

    assert request.user == "client-user"
    assert request.parallel_tool_calls is False
    assert openai_request_to_gemini(request).contents[0].parts[0].text == "hi"


def test_gemini_response_maps_usage_finish_reason_images_and_tools():
    result = gemini_response_to_openai(
        GeminiGenerateContentResponse(
            candidates=[
                GeminiCandidateResponse(
                    content=GeminiContentResponse(
                        parts=[
                            GeminiPartResponse(text="hello"),
                            GeminiPartResponse(
                                functionCall=GeminiFunctionCallPayload(
                                    name="weather",
                                    args={"city": "Shanghai"},
                                    id="call_1",
                                )
                            ),
                        ]
                    ),
                    finishReason="STOP",
                )
            ],
            usageMetadata=GeminiUsageMetadata(
                promptTokenCount=10,
                candidatesTokenCount=5,
                thoughtsTokenCount=2,
                totalTokenCount=15,
            ),
            responseId="resp_1",
        ),
        model="gemini-3.7-flash",
    )
    assert result["object"] == "chat.completion"
    choices = result["choices"]
    assert isinstance(choices, list)
    choice = choices[0]
    assert isinstance(choice, dict)
    assert choice["finish_reason"] == "tool_calls"
    message = choice["message"]
    assert isinstance(message, dict)
    tool_calls = message["tool_calls"]
    assert isinstance(tool_calls, list)
    first_tool_call = tool_calls[0]
    assert isinstance(first_tool_call, dict)
    function = first_tool_call["function"]
    assert isinstance(function, dict)
    assert function["arguments"] == '{"city":"Shanghai"}'
    assert message["content"] == "hello"
    usage = result["usage"]
    assert isinstance(usage, dict)
    details = usage["completion_tokens_details"]
    assert isinstance(details, dict)
    assert details["reasoning_tokens"] == 2


def test_gemini_generated_media_is_rejected_by_openai_response():
    from fastapi import HTTPException

    with pytest.raises(HTTPException, match="generated media"):
        gemini_response_to_openai(
            GeminiGenerateContentResponse(
                candidates=[
                    GeminiCandidateResponse(
                        content=GeminiContentResponse(
                            parts=[
                                GeminiPartResponse(
                                    inlineData=GeminiInlineDataResponse(
                                        mimeType="image/png", data="cG5n"
                                    )
                                )
                            ]
                        )
                    )
                ]
            ),
            model="gemini-image",
        )


@pytest.mark.asyncio
async def test_openai_route_nonstream_and_openai_error(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", frozenset({"secret"}))
    fake_response = GeminiGenerateContentResponse(
        candidates=[
            GeminiCandidateResponse(
                content=GeminiContentResponse(parts=[GeminiPartResponse(text="hello")]),
                finishReason="STOP",
            )
        ],
        usageMetadata=GeminiUsageMetadata(
            promptTokenCount=2, candidatesTokenCount=1, totalTokenCount=3
        ),
    )
    monkeypatch.setattr(
        "aistudio_api.api.routes_openai.handle_gemini_generate_content",
        AsyncMock(return_value=fake_response),
    )
    monkeypatch.setitem(app.dependency_overrides, get_client, lambda: object())
    async with _build_client() as client:
        unauthorized = await client.get("/v1/models")
        assert unauthorized.status_code == 401
        assert unauthorized.json()["error"]["type"] == "authentication_error"
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer secret"},
            json={
                "model": "gemini-3.7-flash",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "hello"
        assert response.json()["usage"]["total_tokens"] == 3


@pytest.mark.asyncio
async def test_openai_stream_translates_sse_and_done_marker():
    from aistudio_api.api.routes_openai import _openai_stream

    async def gemini_events():
        yield b'data: {"candidates":[{"content":{"role":"model","parts":[{"text":"hi"}]},"index":0}]}\n\n'
        yield b'data: {"candidates":[{"content":{"role":"model","parts":[]},"finishReason":"STOP","index":0}],"usageMetadata":{"promptTokenCount":2,"candidatesTokenCount":1,"totalTokenCount":3}}\n\n'

    response = StreamingResponse(gemini_events(), media_type="text/event-stream")
    chunks = [
        chunk
        async for chunk in _openai_stream(
            response, model="gemini-3.7-flash", include_usage=True
        )
    ]
    decoded = "".join(chunks)
    events = [
        line.removeprefix("data: ")
        for line in decoded.splitlines()
        if line.startswith("data: ")
    ]
    payloads = [json.loads(event) for event in events if event != "[DONE]"]
    assert payloads[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert payloads[1]["choices"][0]["delta"]["content"] == "hi"
    assert payloads[-1]["choices"] == []
    assert events[-1] == "[DONE]"


@pytest.mark.asyncio
async def test_delayed_nonstream_error_preserves_http_status():
    from aistudio_api.api.routes_openai import _buffered_openai_response

    async def gemini_error():
        yield b"\n"
        yield b'{"error":{"code":429,"message":"quota exceeded","status":"RESOURCE_EXHAUSTED"}}'

    response = await _buffered_openai_response(
        StreamingResponse(gemini_error(), media_type="application/json"),
        model="gemini-3.7-flash",
    )
    assert response.status_code == 429
    assert (
        response.body
        == b'{"error":{"message":"quota exceeded","type":"rate_limit_error","param":null,"code":429}}'
    )


def test_text_only_roles_reject_non_text_content_parts():
    from fastapi import HTTPException

    request = OpenAIChatCompletionRequest(
        model="gemini-3.7-flash",
        messages=[
            {
                "role": "system",
                "content": [{"type": "input_audio", "input_audio": {}}],
            },
            {"role": "user", "content": "hello"},
        ],
    )
    with pytest.raises(HTTPException, match="Only text content items"):
        openai_request_to_gemini(request)
