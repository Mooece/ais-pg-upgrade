"""Exercise the patch's full-reply path against upstream c770 contracts."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.api.schemas import (
    GeminiContent,
    GeminiGenerateContentRequest,
    GeminiGenerationConfig,
    GeminiPart,
    OpenAIChatCompletionRequest,
)
from aistudio_api.application import api_service_gemini as gemini
from aistudio_api.application.anti_truncation import (
    TOOL_NAME,
    unwrap_response,
    unwrap_stream,
    wrap_request,
)
from aistudio_api.domain.models import (
    Candidate,
    CandidatePart,
    ModelOutput,
    ThoughtSignatureEvent,
)
from aistudio_api.infrastructure.gateway.client import AIStudioClient, PreparedRequest
from aistudio_api.infrastructure.gateway.session import SessionContext


@pytest.fixture
def prepared_client(monkeypatch):
    monkeypatch.setattr(gemini, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(gemini.runtime_state, "account_service", None)
    monkeypatch.setattr(gemini.runtime_state, "rotator", None)
    monkeypatch.setattr(gemini.runtime_state, "record", MagicMock())
    client = MagicMock(spec=AIStudioClient)
    client.prepare_request = AsyncMock(
        return_value=PreparedRequest(
            url="https://example.test/rpc", headers={}, body="[]", ctx=SessionContext()
        )
    )
    return client


def request():
    return wrap_request(
        GeminiGenerateContentRequest(
            contents=[GeminiContent(role="user", parts=[GeminiPart(text="hello")])]
        )
    )


async def response_chunks(response: StreamingResponse) -> list[str]:
    return [
        chunk if isinstance(chunk, str) else bytes(chunk).decode()
        async for chunk in response.body_iterator
    ]


def test_upstream_generation_validation_and_openai_schema_coexist():
    for invalid in (0, -1, 65537, True, "100", 1.5):
        with pytest.raises(ValidationError):
            GeminiGenerationConfig.model_validate({"maxOutputTokens": invalid})
    assert GeminiGenerationConfig(maxOutputTokens=65536).maxOutputTokens == 65536
    assert (
        GeminiGenerateContentRequest.model_validate(
            {"contents": [], "cached_content": "cachedContents/example"}
        ).cachedContent
        == "cachedContents/example"
    )
    assert OpenAIChatCompletionRequest(model="gemini-4-argon", messages=[]).model
    with pytest.raises(ValidationError):
        OpenAIChatCompletionRequest.model_validate(
            {"model": "gemini-4-argon", "messages": [], "unknown_parameter": True}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gemini-3.7-flash", "gemini-4-argon"])
@pytest.mark.parametrize("finish_reason", [None, 0, 22, 999, True])
async def test_full_reply_nonstream_rejects_nonterminal_result(
    prepared_client, model, finish_reason
):
    prepared_client.generate_content = AsyncMock(
        return_value=ModelOutput(
            candidates=[Candidate(text="incomplete", finish_reason=finish_reason)]
        )
    )
    with pytest.raises(HTTPException) as error:
        await gemini.handle_gemini_generate_content(
            model, request(), prepared_client, stream=False, require_finish_reason=True
        )
    assert error.value.status_code == 502
    assert "real finish reason" in str(error.value.detail)
    assert prepared_client.generate_content.await_count == 1
    assert prepared_client.prepare_request.return_value.require_finish_reason is True
    assert isinstance(gemini.runtime_state.record, MagicMock)
    gemini.runtime_state.record.assert_not_called()


@pytest.mark.asyncio
async def test_full_reply_nonstream_preserves_ordered_parts_and_literal_tags(
    prepared_client,
):
    answer = "First paragraph.\n\n<think>literal user-facing markup</think>"
    prepared_client.generate_content = AsyncMock(
        return_value=ModelOutput(
            candidates=[
                Candidate(
                    finish_reason=1,
                    parts=[
                        CandidatePart(
                            kind="thinking",
                            text="reasoning",
                            thought=True,
                            has_text=True,
                        ),
                        CandidatePart(kind="text", text="prefix: ", has_text=True),
                        CandidatePart(
                            kind="function_call",
                            thought_signature="tool-signature",
                            payload={"name": TOOL_NAME, "args": {"content": answer}},
                        ),
                        CandidatePart(kind="text", text=" suffix", has_text=True),
                    ],
                )
            ]
        )
    )
    response = await gemini.handle_gemini_generate_content(
        "gemini-4-argon",
        request(),
        prepared_client,
        stream=False,
        require_finish_reason=True,
    )
    assert isinstance(response, GeminiGenerateContentResponse)
    payload = response.model_dump(mode="json", exclude_none=True)
    parts = payload["candidates"][0]["content"]["parts"]
    assert parts[0] == {"text": "reasoning", "thought": True}
    assert parts[1] == {"text": "prefix: "}
    assert parts[2]["thoughtSignature"] == "tool-signature"
    assert parts[3] == {"text": " suffix"}
    unwrapped = GeminiGenerateContentResponse.model_validate(unwrap_response(payload))
    assert [
        part.model_dump(exclude_none=True)
        for part in unwrapped.candidates[0].content.parts
    ] == [{"text": "prefix: " + answer + " suffix"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", [None, 0, 22, "0", "22", "UNKNOWN", True])
async def test_full_reply_stream_never_synthesizes_success_for_nonterminal_result(
    prepared_client, finish_reason
):
    async def upstream(**_kwargs):
        yield "body", "incomplete"
        if finish_reason is not None:
            yield "finish_reason", finish_reason

    prepared_client.stream_generate_content = MagicMock(side_effect=upstream)
    response = await gemini.handle_gemini_generate_content(
        "gemini-4-argon",
        request(),
        prepared_client,
        stream=True,
        require_finish_reason=True,
    )
    assert isinstance(response, StreamingResponse)
    payloads = [json.loads(chunk[6:]) for chunk in await response_chunks(response)]
    assert payloads[-1]["error"]["code"] == 502
    assert "real finish reason" in payloads[-1]["error"]["message"]
    assert all(
        "finishReason" not in candidate
        for payload in payloads
        for candidate in payload.get("candidates", [])
    )
    assert prepared_client.prepare_request.return_value.require_finish_reason is True
    assert isinstance(gemini.runtime_state.record, MagicMock)
    gemini.runtime_state.record.assert_not_called()


@pytest.mark.asyncio
async def test_full_reply_stream_keeps_upstream_signature_affinity(prepared_client):
    answer = "Complete <think>literal tags</think> answer."

    async def upstream(**_kwargs):
        yield (
            "thought_signature",
            ThoughtSignatureEvent(
                "reasoning-carrier",
                standalone=True,
                thought=True,
                empty_text_carrier=True,
            ),
        )
        yield (
            "candidate_part",
            CandidatePart(
                kind="function_call",
                thought_signature="own-tool-signature",
                payload={"name": TOOL_NAME, "args": {"content": answer}},
            ),
        )
        yield "finish_reason", "STOP"

    prepared_client.stream_generate_content = MagicMock(side_effect=upstream)
    response = await gemini.handle_gemini_generate_content(
        "gemini-4-argon",
        request(),
        prepared_client,
        stream=True,
        require_finish_reason=True,
    )
    assert isinstance(response, StreamingResponse)
    chunks = await response_chunks(response)
    payloads = [json.loads(chunk[6:]) for chunk in chunks]
    parts = [
        part
        for payload in payloads
        for candidate in payload.get("candidates", [])
        for part in candidate.get("content", {}).get("parts", [])
    ]
    assert parts[0] == {
        "text": "",
        "thought": True,
        "thoughtSignature": "reasoning-carrier",
    }
    assert parts[1]["functionCall"]["name"] == TOOL_NAME
    assert parts[1]["thoughtSignature"] == "own-tool-signature"
    assert payloads[-1]["candidates"][0]["finishReason"] == "STOP"

    async def replay():
        for chunk in chunks:
            yield chunk

    unwrapped = [json.loads(chunk[6:]) async for chunk in unwrap_stream(replay())]
    assert unwrapped[0]["candidates"][0]["content"]["parts"] == [{"text": answer}]
