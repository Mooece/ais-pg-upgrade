"""Compatibility checks for the patch against c770 ordered response parts."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from aistudio_api.api import routes_openai as routes
from aistudio_api.api.responses import to_gemini_parts
from aistudio_api.application.anti_truncation import (
    supports_full_reply,
    unwrap_response,
)
from aistudio_api.application.api_service_gemini import format_sse_event
from aistudio_api.domain.models import CandidatePart, ThoughtSignatureEvent


def _payload(parts: list[dict[str, object]]) -> dict[str, object]:
    return {
        "candidates": [
            {"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}
        ]
    }


def _response(parts: list[dict[str, object]]) -> StreamingResponse:
    async def upstream() -> AsyncGenerator[str, None]:
        yield "data: " + json.dumps(_payload(parts)) + "\n\n"

    return StreamingResponse(upstream())


@pytest.mark.parametrize(
    "media_part",
    [
        {
            "fileData": {
                "mimeType": "audio/wav",
                "fileUri": "https://example.test/audio",
            }
        },
        {"videoMetadata": {"fps": 24}},
    ],
)
def test_openai_nonstream_rejects_upstream_media_parts(media_part):
    with pytest.raises(HTTPException, match="generated media") as error:
        routes.gemini_response_to_openai(_payload([media_part]), model="gemini-4-argon")
    assert error.value.status_code == 501


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gemini-4-argon", "gemini-4-argon[防截断]"])
@pytest.mark.parametrize(
    "media_part",
    [
        {
            "fileData": {
                "mimeType": "image/png",
                "fileUri": "https://example.test/image",
            }
        },
        {"videoMetadata": {"fps": 24}},
    ],
)
async def test_openai_stream_rejects_media_parts_without_success_or_cache(
    monkeypatch, media_part, model
):
    save = AsyncMock()
    monkeypatch.setattr(routes, "_save_reply", save)
    output = "".join(
        [
            item
            async for item in routes._openai_stream_events(
                _response([{"text": "partial"}, media_part]),
                model=model,
                include_usage=False,
                cache_owner="test",
            )
        ]
    )
    assert '"error":' in output
    assert "unsupported" in output.lower()
    assert '"finish_reason":"stop"' not in output
    assert output.count("data: [DONE]") == 1
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_reply_nonstream_mixed_video_metadata_cannot_cache(monkeypatch):
    save = AsyncMock()
    monkeypatch.setattr(routes, "_save_reply", save)
    monkeypatch.setattr(
        routes,
        "handle_gemini_generate_content",
        AsyncMock(
            return_value=_payload(
                [
                    {
                        "functionCall": {
                            "name": "aistudio_full_reply",
                            "args": {"content": "complete"},
                        }
                    },
                    {"videoMetadata": {"fps": 24}},
                ]
            )
        ),
    )
    request = routes.OpenAIChatCompletionRequest(
        model="gemini-4-argon[防截断]",
        messages=[{"role": "user", "content": "hello"}],
    )
    with pytest.raises(
        HTTPException, match="unsupported content outside function envelope"
    ):
        await routes._complete_openai_response(
            request,
            routes.openai_request_to_gemini(request),
            MagicMock(spec=routes.AIStudioClient),
            cache_owner="test",
        )
    save.assert_not_awaited()


@pytest.mark.parametrize("function_envelope", [False, True])
@pytest.mark.parametrize(
    "media_part",
    [
        CandidatePart(
            kind="file_data",
            payload={"mimeType": "audio/wav", "fileUri": "https://example.test/audio"},
        ),
        CandidatePart(kind="video_metadata", payload={"fps": 24}),
    ],
)
def test_full_reply_rejects_media_among_otherwise_complete_parts(
    media_part, function_envelope
):
    answer = (
        CandidatePart(
            kind="function_call",
            payload={"name": "aistudio_full_reply", "args": {"content": "complete"}},
        )
        if function_envelope
        else CandidatePart(kind="text", text="complete")
    )
    parts = to_gemini_parts([answer, media_part])
    with pytest.raises(
        HTTPException, match="unsupported content outside function envelope"
    ):
        unwrap_response(
            _payload(
                [part.model_dump(mode="json", exclude_none=True) for part in parts]
            )
        )


def test_full_reply_accepts_complete_envelope_with_safe_standalone_signature():
    result = routes.gemini_response_to_openai(
        _payload(
            [
                {"thoughtSignature": "standalone"},
                {
                    "functionCall": {
                        "name": "aistudio_full_reply",
                        "args": {"content": "complete"},
                    },
                },
            ]
        ),
        model="gemini-4-argon[防截断]",
    )
    choices = result["choices"]
    assert isinstance(choices, list)
    assert choices[0]["message"]["content"] == "complete"
    assert choices[0]["finish_reason"] == "stop"


def test_openai_nonstream_keeps_ordered_text_and_call_signature_affinity():
    parts = to_gemini_parts(
        [
            CandidatePart(kind="thinking", text="private", thought=True),
            CandidatePart(kind="text", text="literal <thought>quoted</thought> "),
            CandidatePart(kind="standalone_signature", thought_signature="standalone"),
            CandidatePart(
                kind="function_call",
                thought_signature="call-sig",
                payload={
                    "name": "weather",
                    "call_id": "call_compat",
                    "args": {"city": "Paris"},
                },
            ),
            CandidatePart(kind="text", text="after"),
        ]
    )
    result = routes.gemini_response_to_openai(
        _payload([part.model_dump(mode="json", exclude_none=True) for part in parts]),
        model="gemini-4-argon",
    )
    choices = result["choices"]
    assert isinstance(choices, list)
    choice = choices[0]
    assert choice["message"]["content"] == "literal <thought>quoted</thought> after"
    call = choice["message"]["tool_calls"][0]
    assert routes._decode_tool_call_id(call["id"], use_cached_signature=False) == (
        "call_compat",
        "call-sig",
    )
    assert choice["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_openai_stream_keeps_call_signature_distinct_from_standalone(monkeypatch):
    monkeypatch.setattr(routes, "_TOOL_CALL_SIGNATURES", routes.OrderedDict())

    async def upstream() -> AsyncGenerator[str, None]:
        for event_type, value in (
            (
                "thought_signature",
                ThoughtSignatureEvent("thought", standalone=True, thought=True),
            ),
            ("thought_signature", ThoughtSignatureEvent("standalone", standalone=True)),
            (
                "candidate_part",
                CandidatePart(
                    kind="function_call",
                    thought_signature="call-sig",
                    payload={
                        "name": "weather",
                        "call_id": "call_stream_compat",
                        "args": {},
                    },
                ),
            ),
        ):
            event = format_sse_event(event_type, value)
            assert event is not None
            yield event
        yield "data: " + json.dumps(_payload([])) + "\n\n"

    chunks = [
        json.loads(line[6:])
        async for event in routes._openai_stream_events(
            StreamingResponse(upstream()), model="gemini-4-argon", include_usage=False
        )
        for line in event.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    calls = [
        call
        for chunk in chunks
        for choice in chunk.get("choices", [])
        for call in choice["delta"].get("tool_calls", [])
    ]
    assert len(calls) == 1
    assert routes._decode_tool_call_id(calls[0]["id"], use_cached_signature=False) == (
        "call_stream_compat",
        "call-sig",
    )
    assert routes._TOOL_CALL_SIGNATURES == {"call_stream_compat": "call-sig"}


@pytest.mark.asyncio
async def test_openai_incomplete_upstream_error_cannot_cache_partial_reply(monkeypatch):
    save = AsyncMock()
    monkeypatch.setattr(routes, "_save_reply", save)

    async def upstream() -> AsyncGenerator[str, None]:
        event = format_sse_event("body", "partial")
        assert event is not None
        yield event
        yield 'data: {"error":{"code":502,"message":"incomplete response"}}\n\n'

    output = "".join(
        [
            event
            async for event in routes._openai_stream_events(
                StreamingResponse(upstream()),
                model="gemini-4-argon",
                include_usage=False,
                cache_owner="test",
            )
        ]
    )
    assert '"content":"partial"' in output
    assert '"code":502' in output
    assert '"finish_reason":"stop"' not in output
    save.assert_not_awaited()


@pytest.mark.parametrize(
    "finish",
    ["SAFETY", "PROHIBITED_CONTENT", "MAX_TOKENS", "FINISH_REASON_UNSPECIFIED"],
)
def test_full_reply_rejects_incomplete_or_blocked_ordered_parts(finish):
    parts = to_gemini_parts(
        [
            CandidatePart(kind="standalone_signature", thought_signature="unrelated"),
            CandidatePart(
                kind="function_call",
                payload={"name": "aistudio_full_reply", "args": {"content": "partial"}},
            ),
        ]
    )
    payload = _payload(
        [part.model_dump(mode="json", exclude_none=True) for part in parts]
    )
    candidates = payload["candidates"]
    assert isinstance(candidates, list)
    candidates[0]["finishReason"] = finish
    with pytest.raises(HTTPException, match="generation did not complete"):
        unwrap_response(payload)


@pytest.mark.parametrize(
    "model", ["gemini-4-argon-image", "gemini-4-audio", "gemini-4-video"]
)
def test_new_media_models_do_not_gain_full_reply_aliases(model):
    assert not supports_full_reply(model)
