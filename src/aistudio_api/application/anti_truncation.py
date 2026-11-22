"""Opt-in full-reply function envelope for text generation."""

from __future__ import annotations

import codecs
import json
import re
from collections.abc import AsyncGenerator, AsyncIterable

from fastapi import HTTPException

from aistudio_api.api.schemas import (
    GeminiContent,
    GeminiGenerateContentRequest,
    GeminiPart,
    GeminiTool,
    OpenAIChatCompletionRequest,
)

ALIAS_SUFFIX = "[防截断]"
TOOL_NAME = "aistudio_full_reply"


def base_model(model: str) -> str:
    return model.removesuffix(ALIAS_SUFFIX)


def supports_full_reply(model: str, metadata: dict[str, object] | None = None) -> bool:
    name = model.removeprefix("models/").lower()
    if not name.startswith("gemini-") or any(
        word in name
        for word in (
            "image",
            "video",
            "audio",
            "tts",
            "live",
            "music",
            "decision",
            "omni",
        )
    ):
        return False
    info = metadata or {}
    if any(
        info.get(flag) is True
        for flag in ("is_image_model", "is_video_model", "is_audio_model")
    ):
        return False
    if info.get("category") == "decision":
        return False
    methods = info.get("supportedGenerationMethods")
    if isinstance(methods, list) and "generateContent" not in methods:
        return False
    capabilities = info.get("capabilities")
    if isinstance(capabilities, list) and capabilities:
        if "function_declarations" not in capabilities:
            return False
        if any(
            cap in capabilities
            for cap in (
                "image_route",
                "video_route",
                "speech_route",
                "live_route",
                "music_route",
            )
        ):
            return False
    return True


def validate_selection(request: OpenAIChatCompletionRequest) -> None:
    if base_model(request.model) == request.model:
        return
    if not supports_full_reply(base_model(request.model)):
        raise HTTPException(
            400,
            detail={
                "message": "Full-reply mode requires a text Gemini model",
                "type": "invalid_request_error",
            },
        )
    if (
        request.tools
        or request.tool_choice is not None
        or request.functions
        or request.function_call is not None
        or request.response_format not in (None, {"type": "text"})
    ):
        raise HTTPException(
            400,
            detail={
                "message": "Full-reply mode cannot be combined with client tools, tool_choice or structured response_format",
                "type": "invalid_request_error",
            },
        )


def wrap_request(request: GeminiGenerateContentRequest) -> GeminiGenerateContentRequest:
    wrapped = request.model_copy(deep=True)
    instruction = GeminiPart(
        text=(
            "Return your complete final answer by calling aistudio_full_reply exactly once. "
            "Put the entire user-facing answer in its content string, preserving all paragraphs, "
            "formatting and dialogue. Do not summarize or leave any answer outside the function. "
            "The function is an output envelope and does not execute any action. "
            "Do not include private reasoning in content. Follow the existing instructions."
        )
    )
    if wrapped.systemInstruction is None:
        wrapped.systemInstruction = GeminiContent(parts=[instruction])
    else:
        wrapped.systemInstruction.parts.append(instruction)
    wrapped.tools = [
        GeminiTool(
            functionDeclarations=[
                {
                    "name": TOOL_NAME,
                    "description": "Return the complete final answer as prose.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {"content": {"type": "STRING"}},
                        "required": ["content"],
                    },
                }
            ]
        )
    ]
    wrapped.toolConfig = {
        "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [TOOL_NAME]}
    }
    return wrapped


def _invalid_reply(message: str) -> HTTPException:
    return HTTPException(
        502,
        detail={
            "message": f"Full-reply response invalid: {message}",
            "type": "upstream_response_error",
        },
    )


def unwrap_response(payload: dict[str, object]) -> dict[str, object]:
    candidates = payload.get("candidates")
    if (
        not isinstance(candidates, list)
        or len(candidates) != 1
        or not isinstance(candidates[0], dict)
    ):
        raise _invalid_reply("expected one candidate")
    candidate = candidates[0]
    reason = candidate.get("finishReason")
    if reason != "STOP":
        raise _invalid_reply(
            f"generation did not complete (finishReason={reason or 'missing'})"
        )
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        raise _invalid_reply("missing function envelope")
    calls: list[dict[str, object]] = []
    for part in parts:
        if not isinstance(part, dict) or part.get("thought") is True:
            continue
        call = part.get("functionCall")
        if part.get("text") or part.get("inlineData"):
            raise _invalid_reply("answer outside function envelope")
        if isinstance(call, dict):
            calls.append(call)
    if len(calls) != 1 or calls[0].get("name") != TOOL_NAME:
        raise _invalid_reply("expected exactly one aistudio_full_reply call")
    args = calls[0].get("args")
    if not isinstance(args, dict) or not isinstance(args.get("content"), str):
        raise _invalid_reply("content must be a string in a complete argument object")
    return {
        **payload,
        "candidates": [
            {
                **candidate,
                "content": {"role": "model", "parts": [{"text": args["content"]}]},
            }
        ],
    }


async def unwrap_stream(
    chunks: AsyncIterable[object],
) -> AsyncGenerator[str, None]:
    """Validate the complete envelope before emitting prose or a success finish."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    parts: list[object] = []
    merged: dict[str, object] = {}
    candidate: dict[str, object] = {}
    async for chunk in chunks:
        if isinstance(chunk, (bytes, bytearray, memoryview)):
            buffer += decoder.decode(chunk)
        elif isinstance(chunk, str):
            buffer += chunk
        else:
            raise _invalid_reply("unsupported SSE body chunk")
        events = re.split(r"\r?\n\r?\n", buffer)
        buffer = events.pop()
        for event in events:
            data = "\n".join(
                line[5:].lstrip()
                for line in event.splitlines()
                if line.startswith("data:")
            )
            if not data or data == "[DONE]":
                continue
            try:
                payload = json.loads(data)
            except json.JSONDecodeError as exc:
                raise _invalid_reply("malformed SSE JSON") from exc
            if not isinstance(payload, dict):
                raise _invalid_reply("invalid SSE response")
            if "error" in payload:
                yield "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
                return
            merged.update(payload)
            candidates = payload.get("candidates")
            if isinstance(candidates, list) and candidates:
                if len(candidates) != 1 or not isinstance(candidates[0], dict):
                    raise _invalid_reply("expected one candidate")
                incoming = candidates[0]
                if candidate.get("finishReason"):
                    raise _invalid_reply("candidate data after completion")
                candidate.update(incoming)
                content = incoming.get("content")
                incoming_parts = (
                    content.get("parts") if isinstance(content, dict) else None
                )
                if isinstance(incoming_parts, list):
                    parts.extend(incoming_parts)
    buffer += decoder.decode(b"", final=True)
    if buffer.strip():
        raise _invalid_reply("incomplete SSE event")
    candidate["content"] = {"parts": parts}
    merged["candidates"] = [candidate]
    yield "data: " + json.dumps(unwrap_response(merged), ensure_ascii=False) + "\n\n"
