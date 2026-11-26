"""Opt-in full-reply function envelope for text generation."""

from __future__ import annotations

import codecs
import json
import re
from collections.abc import AsyncGenerator, AsyncIterable
from dataclasses import dataclass

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


@dataclass(frozen=True)
class FullReplyToolPolicy:
    """Client functions permitted during this one complete generation."""

    client_names: tuple[str, ...] = ()
    allowed_client_names: tuple[str, ...] = ()
    allow_final: bool = True
    envelope_name: str = TOOL_NAME


def _invalid_selection(message: str) -> HTTPException:
    return HTTPException(
        400, detail={"message": message, "type": "invalid_request_error"}
    )


def tool_policy(request: OpenAIChatCompletionRequest) -> FullReplyToolPolicy:
    if request.parallel_tool_calls is True:
        raise _invalid_selection("Full-reply mode supports serial function tools only")
    history_names: set[str] = set()
    for message in request.messages:
        role = message.get("role")
        if (isinstance(role, str) and role.lower() == "function") or message.get(
            "function_call"
        ) is not None:
            raise _invalid_selection(
                "Full-reply mode requires modern tool_calls/tool messages"
            )
        name = message.get("name")
        if isinstance(name, str):
            history_names.add(name)
        calls = message.get("tool_calls")
        if isinstance(calls, list):
            if len(calls) > 1:
                raise _invalid_selection(
                    "Full-reply mode supports serial tool history only"
                )
            for call in calls:
                function = call.get("function") if isinstance(call, dict) else None
                name = function.get("name") if isinstance(function, dict) else None
                if isinstance(name, str):
                    history_names.add(name)
    names: list[str] = []
    for tool in request.tools or []:
        function = tool.get("function")
        if tool.get("type") != "function" or not isinstance(function, dict):
            raise _invalid_selection("Full-reply mode supports function tools only")
        name = function.get("name")
        if not isinstance(name, str) or not name or name.strip() != name:
            raise _invalid_selection(
                "Each function tool requires a nonempty function.name"
            )
        if name in names:
            raise _invalid_selection("Duplicate function tool names are not supported")
        names.append(name)
    declared = tuple(names)
    taken_names = history_names | set(names)
    envelope_name = TOOL_NAME
    suffix = 0
    while envelope_name in taken_names:
        suffix += 1
        envelope_name = f"{TOOL_NAME}_{suffix}"
    choice = request.tool_choice
    if choice is None or choice == "auto":
        return FullReplyToolPolicy(declared, declared, True, envelope_name)
    if choice == "none":
        return FullReplyToolPolicy(declared, (), True, envelope_name)
    if choice == "required":
        if not declared:
            raise _invalid_selection(
                "tool_choice requires at least one declared function tool"
            )
        return FullReplyToolPolicy(declared, declared, False, envelope_name)
    if isinstance(choice, dict) and choice.get("type") == "function":
        function = choice.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if not isinstance(name, str) or name not in declared:
            raise _invalid_selection(
                "tool_choice names a function that was not declared"
            )
        return FullReplyToolPolicy(declared, (name,), False, envelope_name)
    raise _invalid_selection("tool_choice must be auto, none, required, or a function")


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
        request.functions
        or request.function_call is not None
        or request.response_format not in (None, {"type": "text"})
    ):
        raise HTTPException(
            400,
            detail={
                "message": "Full-reply mode cannot be combined with legacy functions/function_call or structured response_format",
                "type": "invalid_request_error",
            },
        )
    tool_policy(request)


def wrap_request(
    request: GeminiGenerateContentRequest,
    policy: FullReplyToolPolicy | None = None,
) -> GeminiGenerateContentRequest:
    policy = policy or FullReplyToolPolicy()
    wrapped = request.model_copy(deep=True)
    instruction = GeminiPart(
        text=(
            f"When returning a final answer, call {policy.envelope_name} exactly once. "
            "Put the entire user-facing answer in its content string, preserving all paragraphs, "
            "formatting and dialogue. Do not summarize or leave any answer outside the function. "
            "The function is an output envelope and does not execute any action. "
            "If a permitted client tool is needed, call exactly one client function instead; "
            "the client will execute it and provide the result in a later request. "
            "Do not mix a client call and the final-answer envelope in the same reply. "
            "Do not include private reasoning in content. Follow the existing instructions."
        )
    )
    if wrapped.systemInstruction is None:
        wrapped.systemInstruction = GeminiContent(parts=[instruction])
    else:
        wrapped.systemInstruction.parts.append(instruction)
    client_tools = [
        GeminiTool(functionDeclarations=tool.functionDeclarations)
        for tool in wrapped.tools or []
        if tool.functionDeclarations
    ]
    actual_names = tuple(
        declaration.get("name")
        for tool in client_tools
        for declaration in tool.functionDeclarations or []
    )
    if actual_names != policy.client_names:
        raise _invalid_selection(
            "Full-reply client declarations do not match the request policy"
        )
    wrapped.tools = [
        *client_tools,
        GeminiTool(
            functionDeclarations=[
                {
                    "name": policy.envelope_name,
                    "description": "Return the complete final answer as prose.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {"content": {"type": "STRING"}},
                        "required": ["content"],
                    },
                }
            ]
        ),
    ]
    allowed_names = list(policy.allowed_client_names)
    if policy.allow_final:
        allowed_names.append(policy.envelope_name)
    wrapped.toolConfig = {
        "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": allowed_names}
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


def unwrap_response(
    payload: dict[str, object], policy: FullReplyToolPolicy | None = None
) -> dict[str, object]:
    policy = policy or FullReplyToolPolicy()
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
    visible_parts: list[dict[str, object]] = []
    calls: list[dict[str, object]] = []
    for part in parts:
        if not isinstance(part, dict):
            raise _invalid_reply("invalid response part")
        if part.get("thought") is True:
            if "functionCall" in part:
                raise _invalid_reply("function call in thought content")
            continue
        visible_parts.append(part)
        call = part.get("functionCall")
        text = part.get("text")
        if text is not None and not isinstance(text, str):
            raise _invalid_reply("visible text must be a string")
        if any(
            part.get(field) is not None
            for field in (
                "inlineData",
                "fileData",
                "videoMetadata",
                "functionResponse",
                "executableCode",
                "codeExecutionResult",
            )
        ):
            raise _invalid_reply("unsupported content outside function envelope")
        if "functionCall" in part:
            if not isinstance(call, dict):
                raise _invalid_reply("invalid function call")
            calls.append(call)
    if calls:
        if len(calls) != 1:
            raise _invalid_reply("expected exactly one function call")
        if calls[0].get("name") != policy.envelope_name:
            if calls[0].get("name") not in policy.allowed_client_names:
                raise _invalid_reply("client function was not permitted by tool_choice")
            call_id = calls[0].get("id")
            if call_id is not None and (not isinstance(call_id, str) or not call_id):
                raise _invalid_reply("client function id must be a nonempty string")
            for part in visible_parts:
                if "functionCall" in part:
                    signature = part.get("thoughtSignature")
                    if signature is not None and not isinstance(signature, str):
                        raise _invalid_reply(
                            "client function signature must be a string"
                        )
            args = calls[0].get("args")
            if not isinstance(args, dict):
                raise _invalid_reply("client arguments must be a complete object")
            try:
                json.dumps(args, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise _invalid_reply(
                    "client arguments must contain valid JSON"
                ) from exc
            return {**payload, "aistudio_full_reply": {"mode": "client_tool_call"}}
        if not policy.allow_final:
            raise _invalid_reply("tool_choice requires a client function call")
        args = calls[0].get("args")
        function_content = args.get("content") if isinstance(args, dict) else None
        if not isinstance(function_content, str):
            raise _invalid_reply(
                "content must be a string in a complete argument object"
            )
        fragments: list[str] = []
        for part in visible_parts:
            text = part.get("text")
            preface = f"调用函数 `{policy.envelope_name}` 输出如下内容。"
            if isinstance(text, str) and text.strip() not in ("", preface):
                fragments.append(text)
            if "functionCall" in part:
                fragments.append(function_content)
        answer = "".join(fragments)
        mode = "function_envelope"
    else:
        if not policy.allow_final:
            raise _invalid_reply("tool_choice requires a client function call")
        plain_fragments: list[str] = []
        for part in visible_parts:
            text = part.get("text")
            if isinstance(text, str):
                plain_fragments.append(text)
        answer = "".join(plain_fragments)
        if not answer.strip():
            raise _invalid_reply("missing function envelope or nonempty plain text")
        mode = "plain_text_fallback"
    return {
        **payload,
        "aistudio_full_reply": {"mode": mode},
        "candidates": [
            {
                **candidate,
                "content": {"role": "model", "parts": [{"text": answer}]},
            }
        ],
    }


async def unwrap_stream(
    chunks: AsyncIterable[object],
    policy: FullReplyToolPolicy | None = None,
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
    yield (
        "data: "
        + json.dumps(unwrap_response(merged, policy), ensure_ascii=False)
        + "\n\n"
    )
