"""OpenAI Chat Completions compatibility routes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
)
from typing import TypeVar, cast

from anyio import CancelScope
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.types import Receive, Scope, Send

from aistudio_api.api.dependencies import (
    _extract_request_token,
    get_client,
    get_runtime_state,
)
from aistudio_api.api.reply_cache import get_reply_cache, reply_cache_available
from aistudio_api.api.reply_cache_response import (
    ReplyCapture,
    replay_chunks,
    replay_completion,
)
from aistudio_api.api.reply_cache_tasks import BufferedReplyStream, reply_cache_tasks
from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.api.schemas import (
    GeminiContent,
    GeminiFunctionCall,
    GeminiFunctionResponse,
    GeminiGenerateContentRequest,
    GeminiGenerationConfig,
    GeminiInlineData,
    GeminiPart,
    GeminiTool,
    OpenAIChatCompletionRequest,
)
from aistudio_api.api.state import RuntimeState
from aistudio_api.application.anti_truncation import (
    ALIAS_SUFFIX,
    base_model,
    supports_full_reply,
    unwrap_response,
    unwrap_stream,
    validate_selection,
    wrap_request,
)
from aistudio_api.application.api_service_gemini import (
    handle_gemini_generate_content,
)
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.model_defaults import get_configured_api_keys
from aistudio_api.infrastructure.utils.logger import get_logger
from aistudio_api.infrastructure.utils.request_diagnostics import log_request_event

logger = get_logger("routes.openai")
router = APIRouter()
_DATA_URL_RE = re.compile(r"^data:([^;,]+);base64,(.*)$", re.DOTALL)
_TOOL_CALL_SIGNATURES: OrderedDict[str, str] = OrderedDict()
_MAX_TOOL_CALL_SIGNATURES = 2048
_CALL_SIGNATURE_MARKER = "--sig-"
_SKIP_THOUGHT_SIGNATURE_VALIDATOR = "skip_thought_signature_validator"
_KEEPALIVE_INTERVAL = 10.0
_T = TypeVar("_T")


async def _close_iterator(iterator: object) -> None:
    close = cast(
        Callable[[], Awaitable[object]] | None, getattr(iterator, "aclose", None)
    )
    if close is not None:
        await close()


async def _cancel_and_wait(task: asyncio.Task[_T]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _with_keepalive(
    iterator: AsyncIterator[_T], heartbeat: _T
) -> AsyncGenerator[_T, None]:
    """Keep one outstanding read alive; an idle interval must not cancel it."""

    async def read_next() -> _T:
        return await anext(iterator)

    pending: asyncio.Task[_T] | None = None
    try:
        while True:
            pending = asyncio.create_task(read_next())
            while True:
                done, _ = await asyncio.wait({pending}, timeout=_KEEPALIVE_INTERVAL)
                if done:
                    break
                yield heartbeat
            try:
                chunk = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield chunk
    finally:
        # Starlette's disconnect cancel scope also covers awaited cleanup.
        with CancelScope(shield=True):
            try:
                if pending is not None:
                    await _cancel_and_wait(pending)
            finally:
                await _close_iterator(iterator)


class _OpenAIStreamingResponse(StreamingResponse):
    """Own a pending result even if the connection closes before body iteration."""

    def __init__(
        self,
        content: AsyncGenerator[str | bytes, None],
        *,
        media_type: str,
        result_task: asyncio.Task[JSONResponse] | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(
            content,
            media_type=media_type,
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
        # Starlette versions used by the project may defer assigning the
        # iterator until ASGI dispatch; tests and callers also consume the
        # response body directly, so keep the source available immediately.
        self.body_iterator = content
        self._iterator = content
        self._result_task = result_task
        self._on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            if self._on_close is not None:
                self._on_close()
            with CancelScope(shield=True):
                try:
                    await self._iterator.aclose()
                finally:
                    if self._result_task is not None:
                        await _cancel_and_wait(self._result_task)
            log_request_event("openai_response_closed")


def _reply_cache_owner(request: Request) -> str | None:
    if not reply_cache_available():
        return None
    token = _extract_request_token(request)
    if token and token in (get_configured_api_keys() | settings.api_keys):
        return hashlib.sha256(token.encode()).hexdigest()
    return None


async def _save_reply(owner: str, completion: dict[str, object] | None) -> None:
    if completion is None:
        return
    try:
        await get_reply_cache().save(owner, completion)
        log_request_event("reply_cache_saved")
    except Exception as exc:
        log_request_event("reply_cache_write_failed", exception_type=type(exc).__name__)
        logger.warning("回复缓存写入失败: %s", type(exc).__name__)


def _remember_tool_signature(call_id: str, signature: object) -> None:
    if not isinstance(signature, str) or not signature:
        return
    _TOOL_CALL_SIGNATURES[call_id] = signature
    _TOOL_CALL_SIGNATURES.move_to_end(call_id)
    while len(_TOOL_CALL_SIGNATURES) > _MAX_TOOL_CALL_SIGNATURES:
        _TOOL_CALL_SIGNATURES.popitem(last=False)


def _encode_tool_call_id(call_id: str, signature: object) -> str:
    if not isinstance(signature, str) or not signature:
        return call_id
    encoded = base64.urlsafe_b64encode(signature.encode("utf-8")).decode("ascii")
    return f"{call_id}{_CALL_SIGNATURE_MARKER}{encoded.rstrip('=')}"


def _decode_tool_call_id(call_id: str) -> tuple[str, str | None]:
    if _CALL_SIGNATURE_MARKER in call_id:
        original, encoded = call_id.rsplit(_CALL_SIGNATURE_MARKER, 1)
        if original and encoded:
            try:
                padded = encoded + "=" * (-len(encoded) % 4)
                signature = base64.urlsafe_b64decode(padded).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                pass
            else:
                return original, signature
    return call_id, _TOOL_CALL_SIGNATURES.get(call_id)


def _explicit_tool_signature(*sources: dict[str, object]) -> str | None:
    """Read opaque signatures preserved separately by OpenAI-compatible clients."""
    nested_sources: list[dict[str, object]] = []
    for source in sources:
        # Some OpenAI-compatible clients put provider metadata in the standard
        # ``extra_content.google`` envelope instead of on the tool call itself.
        for envelope_key in ("extra_content", "extraContent"):
            envelope = source.get(envelope_key)
            if isinstance(envelope, dict):
                nested_sources.append(envelope)
                google = envelope.get("google")
                if isinstance(google, dict):
                    nested_sources.append(google)
        google = source.get("google")
        if isinstance(google, dict):
            nested_sources.append(google)
    for source in (*sources, *nested_sources):
        for key in ("signature", "thought_signature", "thoughtSignature"):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _invalid_request(message: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"message": message, "type": "invalid_request_error"},
    )


def _text_from_content(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        texts: list[str] = []
        for part in value:
            if not isinstance(part, dict) or part.get("type") not in (
                "text",
                "input_text",
            ):
                raise _invalid_request(
                    "Only text content items are supported in this message"
                )
            text = part.get("text")
            if not isinstance(text, str):
                raise _invalid_request("Text content items require a text string")
            texts.append(text)
        return "".join(texts)
    raise _invalid_request("Message content must be a string, array, or null")


def _message_parts(value: object) -> list[GeminiPart]:
    if value is None:
        return []
    if isinstance(value, str):
        return [GeminiPart(text=value)]
    if not isinstance(value, list):
        raise _invalid_request("Message content must be a string, array, or null")

    parts: list[GeminiPart] = []
    for item in value:
        if not isinstance(item, dict):
            raise _invalid_request("Each content item must be an object")
        item_type = item.get("type")
        if item_type in ("text", "input_text"):
            text = item.get("text")
            if not isinstance(text, str):
                raise _invalid_request("Text content items require a text string")
            parts.append(GeminiPart(text=text))
            continue
        if item_type not in ("image_url", "input_image"):
            raise _invalid_request(f"Unsupported message content type: {item_type}")
        image_url = item.get("image_url")
        raw_url = image_url.get("url") if isinstance(image_url, dict) else image_url
        if not isinstance(raw_url, str):
            raise _invalid_request("image_url must contain a URL string")
        match = _DATA_URL_RE.fullmatch(raw_url)
        if match is None:
            raise _invalid_request(
                "Only base64 data URLs are supported for image_url; remote image URLs are not fetched"
            )
        mime_type, encoded = match.groups()
        if not mime_type.lower().startswith("image/"):
            raise _invalid_request("image_url data URLs must use an image MIME type")
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise _invalid_request("image_url contains invalid base64 data") from exc
        parts.append(
            GeminiPart(
                inlineData=GeminiInlineData(
                    mimeType=mime_type,
                    data=base64.b64encode(image_bytes).decode("ascii"),
                )
            )
        )
    return parts


def _function_args(value: object) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise _invalid_request(
                "Function arguments must contain valid JSON"
            ) from exc
    if value is None:
        return {}
    return value


def _openai_tools(
    raw_tools: list[dict[str, object]] | None,
) -> list[GeminiTool] | None:
    if raw_tools is None:
        return None
    tools: list[GeminiTool] = []
    for item in raw_tools:
        if item.get("type") != "function":
            raise _invalid_request("Only function tools are supported")
        function = item.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise _invalid_request("Each function tool requires function.name")
        tools.append(GeminiTool(functionDeclarations=[function]))
    return tools


def _tool_config(
    choice: str | dict[str, object] | None,
    tools: list[GeminiTool] | None,
) -> dict[str, object] | None:
    if choice is None:
        return None
    mode: str
    allowed_names: list[str] = []
    if isinstance(choice, str):
        mode = {"none": "NONE", "auto": "AUTO", "required": "ANY"}.get(choice, "")
    elif isinstance(choice, dict) and choice.get("type") == "function":
        function = choice.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if not isinstance(name, str) or not name:
            raise _invalid_request("Named tool_choice requires function.name")
        mode = "ANY"
        allowed_names = [name]
    else:
        mode = ""
    if not mode:
        raise _invalid_request(
            "tool_choice must be auto, none, required, or a function"
        )
    if mode != "NONE" and not tools:
        raise _invalid_request(
            "tool_choice requires at least one declared function tool"
        )
    declared_names = {
        str(declaration.get("name"))
        for tool in tools or []
        for declaration in tool.functionDeclarations or []
        if isinstance(declaration, dict) and declaration.get("name")
    }
    if allowed_names and allowed_names[0] not in declared_names:
        raise _invalid_request("tool_choice names a function that was not declared")
    function_config: dict[str, object] = {"mode": mode}
    if allowed_names:
        function_config["allowedFunctionNames"] = allowed_names
    return {"functionCallingConfig": function_config}


def openai_request_to_gemini(
    request: OpenAIChatCompletionRequest,
) -> GeminiGenerateContentRequest:
    if request.n not in (None, 1):
        raise _invalid_request("Only n=1 is supported")
    if request.logprobs is True or (request.top_logprobs or 0) > 0:
        raise _invalid_request("Log probabilities are not supported")
    if request.parallel_tool_calls is True:
        raise _invalid_request("parallel_tool_calls=true is not supported")
    if request.logit_bias:
        raise _invalid_request("logit_bias is not supported")
    if request.modalities and any(
        modality != "text" for modality in request.modalities
    ):
        raise _invalid_request("Only text output modality is supported")
    if request.functions is not None or request.function_call is not None:
        raise _invalid_request(
            "Legacy functions/function_call are not supported; use tools/tool_choice"
        )
    if request.audio is not None:
        raise _invalid_request("Audio output is not supported")
    if request.prediction is not None:
        raise _invalid_request("Prediction output is not supported")
    if request.web_search_options is not None:
        raise _invalid_request("web_search_options is not supported")
    if not request.messages:
        raise _invalid_request("messages must contain at least one message")

    system_text: list[str] = []
    contents: list[GeminiContent] = []
    function_names: dict[str, str] = {}
    for message in request.messages:
        role = message.get("role")
        if not isinstance(role, str):
            raise _invalid_request("Each message requires a role")
        role = role.lower()
        content = message.get("content")
        if role in ("system", "developer"):
            text = _text_from_content(content)
            if text:
                system_text.append(text)
            continue
        if role in ("tool", "function"):
            call_id = message.get("tool_call_id")
            name = message.get("name")
            decoded_call_id: str | None = call_id if isinstance(call_id, str) else None
            if isinstance(call_id, str):
                decoded_call_id, _ = _decode_tool_call_id(call_id)
                name = function_names.get(
                    call_id, function_names.get(decoded_call_id, name)
                )
            if not isinstance(name, str) or not name:
                raise _invalid_request(
                    "Tool results require a known tool_call_id or name"
                )
            result = _text_from_content(content)
            contents.append(
                GeminiContent(
                    role="user",
                    parts=[
                        GeminiPart(
                            functionResponse=GeminiFunctionResponse(
                                name=name,
                                response={"result": result},
                                id=decoded_call_id,
                            )
                        )
                    ],
                )
            )
            continue
        if role not in ("user", "assistant"):
            raise _invalid_request(f"Unsupported message role: {role}")

        parts = _message_parts(content)
        gemini_role = "model" if role == "assistant" else "user"
        raw_calls = message.get("tool_calls")
        if raw_calls is not None:
            if not isinstance(raw_calls, list):
                raise _invalid_request("tool_calls must be an array")
            if role != "assistant":
                raise _invalid_request("Only assistant messages can contain tool_calls")
            for raw_call in raw_calls:
                if not isinstance(raw_call, dict):
                    raise _invalid_request("Each tool_call must be an object")
                if raw_call.get("type") != "function":
                    raise _invalid_request("Only function tool_calls are supported")
                function = raw_call.get("function")
                if not isinstance(function, dict):
                    raise _invalid_request("Each tool_call requires a function object")
                name = function.get("name")
                call_id = raw_call.get("id")
                if not isinstance(name, str) or not name:
                    raise _invalid_request("Each tool_call requires function.name")
                if not isinstance(call_id, str) or not call_id:
                    call_id = f"call_{uuid.uuid4().hex[:20]}"
                raw_call_id, signature = _decode_tool_call_id(call_id)
                signature = _explicit_tool_signature(raw_call, function) or signature
                if signature is None:
                    # OpenAI clients may discard provider metadata from synthetic or
                    # imported tool calls. Google supports this marker for calls
                    # without a provider-issued signature; use it only as a fallback.
                    signature = _SKIP_THOUGHT_SIGNATURE_VALIDATOR
                function_names[call_id] = name
                parts.append(
                    GeminiPart(
                        functionCall=GeminiFunctionCall(
                            name=name,
                            args=_function_args(function.get("arguments")),
                            id=raw_call_id,
                        ),
                        thoughtSignature=signature,
                    )
                )

        legacy_call = message.get("function_call")
        if legacy_call is not None:
            if role != "assistant" or not isinstance(legacy_call, dict):
                raise _invalid_request(
                    "function_call is only valid on assistant messages"
                )
            name = legacy_call.get("name")
            if not isinstance(name, str) or not name:
                raise _invalid_request("function_call requires a name")
            call_id = f"call_{uuid.uuid4().hex[:20]}"
            function_names[call_id] = name
            parts.append(
                GeminiPart(
                    functionCall=GeminiFunctionCall(
                        name=name,
                        args=_function_args(legacy_call.get("arguments")),
                        id=call_id,
                    ),
                    thoughtSignature=_TOOL_CALL_SIGNATURES.get(call_id),
                )
            )
        if not parts:
            parts.append(GeminiPart(text=""))
        contents.append(GeminiContent(role=gemini_role, parts=parts))

    if not contents:
        raise _invalid_request(
            "messages must contain a user, assistant, or tool message"
        )
    gemini_tools = _openai_tools(request.tools)
    generation_config = GeminiGenerationConfig(
        temperature=request.temperature,
        topP=request.top_p,
        maxOutputTokens=(
            request.max_completion_tokens
            if request.max_completion_tokens is not None
            else request.max_tokens
        ),
        stopSequences=[request.stop] if isinstance(request.stop, str) else request.stop,
        presencePenalty=request.presence_penalty,
        frequencyPenalty=request.frequency_penalty,
        seed=request.seed,
        logprobs=None,
    )
    response_format = request.response_format or {}
    response_schema: dict[str, object] | None = None
    if response_format.get("type") == "json_object":
        generation_config.responseMimeType = "application/json"
    elif response_format.get("type") == "json_schema":
        schema_config = response_format.get("json_schema")
        candidate = (
            schema_config.get("schema") if isinstance(schema_config, dict) else None
        )
        if not isinstance(candidate, dict):
            raise _invalid_request(
                "json_schema response_format requires json_schema.schema"
            )
        response_schema = candidate
        generation_config.responseMimeType = "application/json"
        generation_config.responseSchema = response_schema
    elif response_format and response_format.get("type") not in (None, "text"):
        raise _invalid_request("Unsupported response_format type")

    return GeminiGenerateContentRequest(
        contents=contents,
        systemInstruction=(
            GeminiContent(
                role="user", parts=[GeminiPart(text="\n\n".join(system_text))]
            )
            if system_text
            else None
        ),
        tools=gemini_tools,
        toolConfig=_tool_config(request.tool_choice, gemini_tools),
        generationConfig=generation_config,
        reasoningEffort=request.reasoning_effort,
    )


def _as_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result = dump(mode="json", by_alias=True, exclude_none=True)
        return result if isinstance(result, dict) else {}
    return {}


def _openai_usage(raw_usage: object) -> dict[str, object]:
    usage = _as_dict(raw_usage)
    prompt_tokens = _coerce_int(usage.get("promptTokenCount"))
    completion_tokens = _coerce_int(usage.get("candidatesTokenCount"))
    result: dict[str, object] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": int(
            _coerce_int(usage.get("totalTokenCount"))
            or prompt_tokens + completion_tokens
        ),
    }
    reasoning_tokens = _coerce_int(usage.get("thoughtsTokenCount"))
    cached_tokens = _coerce_int(usage.get("cachedContentTokenCount"))
    if reasoning_tokens:
        result["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
    if cached_tokens:
        result["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    return result


def _coerce_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _openai_finish_reason(reason: object) -> str:
    normalized = str(reason or "STOP").upper()
    if normalized in ("MAX_TOKENS", "MAX_OUTPUT_TOKENS"):
        return "length"
    if normalized in ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT"):
        return "content_filter"
    return "stop"


def gemini_response_to_openai(
    response: GeminiGenerateContentResponse | dict[str, object],
    *,
    model: str,
) -> dict[str, object]:
    payload = _as_dict(response)
    if base_model(model) != model:
        payload = unwrap_response(payload)
    candidates = payload.get("candidates")
    candidate = (
        _as_dict(candidates[0]) if isinstance(candidates, list) and candidates else {}
    )
    content_obj = _as_dict(candidate.get("content"))
    raw_parts = content_obj.get("parts")
    parts = raw_parts if isinstance(raw_parts, list) else []
    text_parts: list[str] = []
    tool_calls: list[dict[str, object]] = []
    for raw_part in parts:
        part = _as_dict(raw_part)
        if part.get("thought") is True:
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            text_parts.append(text)
        function_call = _as_dict(part.get("functionCall"))
        if function_call:
            name = str(function_call.get("name") or "unknown")
            raw_call_id = str(
                function_call.get("id") or f"call_{uuid.uuid4().hex[:20]}"
            )
            signature = part.get("thoughtSignature")
            _remember_tool_signature(raw_call_id, signature)
            call_id = _encode_tool_call_id(raw_call_id, signature)
            args = function_call.get("args")
            tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(
                            args if args is not None else {},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                }
            )
        inline_data = _as_dict(part.get("inlineData"))
        if inline_data.get("mimeType") and inline_data.get("data"):
            raise HTTPException(
                status_code=501,
                detail={
                    "message": "OpenAI Chat Completions does not support generated media; use the native Gemini API",
                    "type": "unsupported_feature",
                },
            )
    content: str | None = "".join(text_parts) or None
    choice_finish_reason = (
        "tool_calls"
        if tool_calls
        else _openai_finish_reason(candidate.get("finishReason"))
    )
    message: dict[str, object] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    response_id = str(payload.get("responseId") or f"chatcmpl-{uuid.uuid4().hex}")
    return {
        "id": response_id
        if response_id.startswith("chatcmpl-")
        else f"chatcmpl-{response_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model.removeprefix("models/"),
        "choices": [
            {"index": 0, "message": message, "finish_reason": choice_finish_reason}
        ],
        "usage": _openai_usage(payload.get("usageMetadata")),
    }


def _sse(payload: dict[str, object]) -> str:
    return (
        "data: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    )


def _chat_chunk(
    *,
    completion_id: str,
    created: int,
    model: str,
    delta: dict[str, object],
    finish_reason: str | None = None,
) -> dict[str, object]:
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model.removeprefix("models/"),
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _openai_stream_error(
    message: str, error_type: str = "server_error", *, code: int | str | None = None
) -> dict[str, object]:
    return {
        "error": {"message": message, "type": error_type, "param": None, "code": code}
    }


async def _openai_stream(
    response: StreamingResponse,
    *,
    model: str,
    include_usage: bool,
) -> AsyncGenerator[str, None]:
    events = _openai_stream_events(response, model=model, include_usage=include_usage)
    kept_events = _with_keepalive(events, ": keepalive\n\n")
    try:
        async for chunk in kept_events:
            yield chunk
    finally:
        with CancelScope(shield=True):
            await kept_events.aclose()


async def _openai_stream_events(
    response: StreamingResponse,
    *,
    model: str,
    include_usage: bool,
    cache_owner: str | None = None,
) -> AsyncGenerator[str, None]:
    log_request_event("openai_stream_started")
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    yield _sse(
        _chat_chunk(
            completion_id=completion_id,
            created=created,
            model=model,
            delta={"role": "assistant"},
        )
    )
    finish_reason = "stop"
    final_usage: dict[str, object] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    has_tool_calls = False
    capture = ReplyCapture() if cache_owner else None
    upstream_finished = False
    tool_indexes: dict[str, int] = {}
    tool_response_ids: dict[str, str] = {}
    started_tool_calls: set[str] = set()
    next_tool_index = 0
    sse_buffer = ""
    iterator = response.body_iterator
    chunks = unwrap_stream(iterator) if base_model(model) != model else iterator
    try:
        async for raw_chunk in chunks:
            chunk_text = (
                raw_chunk.decode("utf-8", errors="replace")
                if isinstance(raw_chunk, bytes)
                else str(raw_chunk)
            )
            sse_buffer += chunk_text
            events = re.split(r"\r?\n\r?\n", sse_buffer)
            sse_buffer = events.pop()
            for event in events:
                data_lines = [
                    line[5:].lstrip()
                    for line in event.splitlines()
                    if line.startswith("data:")
                ]
                raw_data = "\n".join(data_lines).strip()
                if not raw_data or raw_data == "[DONE]":
                    continue
                try:
                    payload = json.loads(raw_data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                if isinstance(payload.get("error"), dict):
                    error = payload["error"]
                    message = str(error.get("message") or "Upstream request failed")
                    code = error.get("code")
                    numeric_code = code if isinstance(code, int) else 500
                    default_type = (
                        "authentication_error"
                        if numeric_code == 401
                        else "permission_error"
                        if numeric_code == 403
                        else "rate_limit_error"
                        if numeric_code == 429
                        else "invalid_request_error"
                        if numeric_code < 500
                        else "server_error"
                    )
                    yield _sse(
                        _openai_stream_error(
                            message,
                            str(error.get("type") or default_type),
                            code=numeric_code,
                        )
                    )
                    yield "data: [DONE]\n\n"
                    return
                usage_meta = payload.get("usageMetadata")
                if isinstance(usage_meta, dict):
                    final_usage = _openai_usage(usage_meta)
                candidates = payload.get("candidates")
                candidate = (
                    _as_dict(candidates[0])
                    if isinstance(candidates, list) and candidates
                    else {}
                )
                raw_reason = candidate.get("finishReason")
                if raw_reason:
                    upstream_finished = True
                    finish_reason = _openai_finish_reason(raw_reason)
                content_obj = _as_dict(candidate.get("content"))
                raw_parts = content_obj.get("parts")
                delta: dict[str, object] = {}
                text_fragments: list[str] = []
                calls: list[dict[str, object]] = []
                for raw_part in raw_parts if isinstance(raw_parts, list) else []:
                    part = _as_dict(raw_part)
                    if part.get("thought") is True:
                        continue
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        text_fragments.append(text)
                    function_call = _as_dict(part.get("functionCall"))
                    if function_call:
                        has_tool_calls = True
                        function_name = str(function_call.get("name") or "unknown")
                        raw_call_id = str(
                            function_call.get("id") or f"stream:{function_name}"
                        )
                        _remember_tool_signature(
                            raw_call_id, part.get("thoughtSignature")
                        )
                        if raw_call_id not in tool_indexes:
                            tool_indexes[raw_call_id] = next_tool_index
                            original_call_id = (
                                str(function_call.get("id"))
                                if function_call.get("id")
                                else f"call_{uuid.uuid4().hex[:20]}"
                            )
                            tool_response_ids[raw_call_id] = _encode_tool_call_id(
                                original_call_id, part.get("thoughtSignature")
                            )
                            next_tool_index += 1
                        args = function_call.get("args")
                        function_delta: dict[str, object] = {
                            "arguments": json.dumps(
                                args if args is not None else {},
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        }
                        call_delta: dict[str, object] = {
                            "index": tool_indexes[raw_call_id],
                            "function": function_delta,
                        }
                        if raw_call_id not in started_tool_calls:
                            call_delta.update(
                                {
                                    "id": tool_response_ids[raw_call_id],
                                    "type": "function",
                                }
                            )
                            function_delta["name"] = function_name
                            started_tool_calls.add(raw_call_id)
                        calls.append(call_delta)
                    inline_data = _as_dict(part.get("inlineData"))
                    if inline_data.get("mimeType") and inline_data.get("data"):
                        yield _sse(
                            _openai_stream_error(
                                "OpenAI Chat Completions does not support generated media; use the native Gemini API",
                                "unsupported_feature",
                            )
                        )
                        yield "data: [DONE]\n\n"
                        return
                if text_fragments:
                    delta["content"] = "".join(text_fragments)
                if calls:
                    delta["tool_calls"] = calls
                if delta:
                    if capture is not None:
                        capture.add_delta(delta)
                    yield _sse(
                        _chat_chunk(
                            completion_id=completion_id,
                            created=created,
                            model=model,
                            delta=delta,
                        )
                    )
    except asyncio.CancelledError:
        log_request_event("openai_stream_cancelled")
        raise
    except Exception as exc:
        log_request_event(
            "openai_stream_error",
            exception_type=type(exc).__name__,
            exception_errno=getattr(exc, "errno", None),
        )
        logger.warning("OpenAI 兼容流式请求失败: %s", type(exc).__name__)
        yield _sse(
            _late_openai_error(exc)
            if base_model(model) != model
            else _openai_stream_error(str(exc))
        )
        yield "data: [DONE]\n\n"
        return
    finally:
        with CancelScope(shield=True):
            if base_model(model) != model:
                await _close_iterator(chunks)
            await _close_iterator(iterator)
        log_request_event("openai_stream_iterator_closed")

    if has_tool_calls:
        finish_reason = "tool_calls"
    if capture is not None and cache_owner and upstream_finished:
        await _save_reply(
            cache_owner,
            capture.complete(
                completion_id=completion_id,
                created=created,
                model=model.removeprefix("models/"),
                finish_reason=finish_reason,
                usage=final_usage,
            ),
        )
    yield _sse(
        _chat_chunk(
            completion_id=completion_id,
            created=created,
            model=model,
            delta={},
            finish_reason=finish_reason,
        )
    )
    if include_usage:
        yield _sse(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model.removeprefix("models/"),
                "choices": [],
                "usage": final_usage,
            }
        )
    yield "data: [DONE]\n\n"
    log_request_event("openai_stream_completed")


def _error_type_for_status(status_code: int) -> str:
    return (
        "authentication_error"
        if status_code == 401
        else "permission_error"
        if status_code == 403
        else "rate_limit_error"
        if status_code == 429
        else "invalid_request_error"
        if status_code < 500
        else "server_error"
    )


def _late_openai_error(exc: Exception) -> dict[str, object]:
    status_code = exc.status_code if isinstance(exc, HTTPException) else 502
    detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
    error = detail if isinstance(detail, dict) else {}
    return _openai_stream_error(
        str(error.get("message") or detail or "Request failed"),
        str(error.get("type") or _error_type_for_status(status_code)),
        code=status_code,
    )


async def _buffered_openai_response(
    response: StreamingResponse,
    *,
    model: str,
) -> JSONResponse:
    buffer = bytearray()
    iterator = response.body_iterator
    try:
        async for chunk in iterator:
            raw = chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")
            if raw.strip():
                buffer.extend(raw)
    finally:
        with CancelScope(shield=True):
            await _close_iterator(iterator)

    try:
        payload = json.loads(buffer.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=502,
            detail={"message": "Invalid response from generation service"},
        ) from exc
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        error = payload["error"]
        code_value = error.get("code")
        status_code = (
            code_value
            if isinstance(code_value, int) and 400 <= code_value <= 599
            else 500
        )
        message = str(error.get("message") or "Request failed")
        error_type = str(error.get("type") or _error_type_for_status(status_code))
        return JSONResponse(
            status_code=status_code,
            content=_openai_stream_error(message, error_type, code=status_code),
        )
    return JSONResponse(content=gemini_response_to_openai(payload, model=model))


async def _complete_openai_response(
    request: OpenAIChatCompletionRequest,
    gemini_request: GeminiGenerateContentRequest,
    client: AIStudioClient,
    *,
    cache_owner: str | None = None,
) -> JSONResponse:
    result = await handle_gemini_generate_content(
        base_model(request.model),
        gemini_request,
        client,
        stream=False,
        **(
            {"require_finish_reason": True}
            if base_model(request.model) != request.model
            else {}
        ),
    )
    if isinstance(result, StreamingResponse):
        response = await _buffered_openai_response(result, model=request.model)
    else:
        response = JSONResponse(
            content=gemini_response_to_openai(result, model=request.model)
        )
    if cache_owner and response.status_code == 200:
        await _save_reply(cache_owner, json.loads(bytes(response.body)))
    return response


async def _nonstream_openai_response(
    request: OpenAIChatCompletionRequest,
    gemini_request: GeminiGenerateContentRequest,
    client: AIStudioClient,
    *,
    cache_owner: str | None = None,
) -> JSONResponse | StreamingResponse:
    # Cover preparation, account selection and the inner Gemini wait with one
    # deadline. The generation task is never cancelled just to emit whitespace.
    def generate() -> Coroutine[object, object, JSONResponse]:
        return _complete_openai_response(
            request, gemini_request, client, cache_owner=cache_owner
        )

    task = reply_cache_tasks.start(generate) if cache_owner else None
    detached = task is not None
    if task is None:
        task = asyncio.create_task(generate())
    try:
        done, _ = await asyncio.wait({task}, timeout=_KEEPALIVE_INTERVAL)
        if done:
            return task.result()

        async def json_with_keepalive() -> AsyncGenerator[str | bytes, None]:
            log_request_event("openai_nonstream_keepalive_started")
            try:
                yield b"\n"
                while not task.done():
                    done, _ = await asyncio.wait({task}, timeout=_KEEPALIVE_INTERVAL)
                    if not done:
                        yield b"\n"
                result = task.result()
                yield bytes(result.body)
                log_request_event(
                    "openai_nonstream_completed", result_status=result.status_code
                )
            except asyncio.CancelledError:
                log_request_event("openai_nonstream_cancelled")
                raise
            except Exception as exc:
                log_request_event(
                    "openai_nonstream_late_error",
                    exception_type=type(exc).__name__,
                    exception_errno=getattr(exc, "errno", None),
                )
                yield json.dumps(_late_openai_error(exc), ensure_ascii=False).encode()
            finally:
                with CancelScope(shield=True):
                    if not detached:
                        await _cancel_and_wait(task)

        return _OpenAIStreamingResponse(
            json_with_keepalive(),
            media_type="application/json",
            result_task=None if detached else task,
        )
    except BaseException:
        with CancelScope(shield=True):
            if not detached:
                await _cancel_and_wait(task)
        raise


async def _prepared_openai_events(
    request: OpenAIChatCompletionRequest,
    gemini_request: GeminiGenerateContentRequest,
    client: AIStudioClient,
    *,
    cache_owner: str | None = None,
) -> AsyncGenerator[str, None]:
    try:
        log_request_event("openai_stream_preparing")
        result = await handle_gemini_generate_content(
            base_model(request.model),
            gemini_request,
            client,
            stream=True,
            **(
                {"require_finish_reason": True}
                if base_model(request.model) != request.model
                else {}
            ),
        )
        if not isinstance(result, StreamingResponse):
            raise HTTPException(
                500, detail={"message": "Streaming response was not created"}
            )
        options = request.stream_options or {}
        events = _openai_stream_events(
            result,
            model=request.model,
            include_usage=options.get("include_usage") is True,
            cache_owner=cache_owner,
        )
        try:
            async for chunk in events:
                yield chunk
        finally:
            with CancelScope(shield=True):
                await events.aclose()
                await _close_iterator(result.body_iterator)
    except asyncio.CancelledError:
        log_request_event("openai_stream_preparation_or_iteration_cancelled")
        raise
    except Exception as exc:
        log_request_event(
            "openai_stream_preparation_error",
            exception_type=type(exc).__name__,
            exception_errno=getattr(exc, "errno", None),
        )
        yield _sse(_late_openai_error(exc))
        yield "data: [DONE]\n\n"


@router.post("/v1/chat/completions")
async def create_chat_completion(
    request: OpenAIChatCompletionRequest,
    client: AIStudioClient = Depends(get_client),
    cache_owner: str | None = Depends(_reply_cache_owner),
):
    try:
        validate_selection(request)
        gemini_request = openai_request_to_gemini(request)
        if base_model(request.model) != request.model:
            gemini_request = wrap_request(gemini_request)
    except HTTPException:
        raise
    except (TypeError, ValueError) as exc:
        raise _invalid_request(str(exc)) from exc

    logger.debug(
        "OpenAI 兼容请求: model=%s, messages=%d", request.model, len(request.messages)
    )
    owner = cache_owner if isinstance(cache_owner, str) else None
    if owner:
        try:
            cached = await get_reply_cache().take(owner)
            if cached is not None:
                log_request_event("reply_cache_replayed")
                completion = replay_completion(cached)
                if request.stream:
                    response = _OpenAIStreamingResponse(
                        replay_chunks(
                            completion,
                            include_usage=(request.stream_options or {}).get(
                                "include_usage"
                            )
                            is True,
                        ),
                        media_type="text/event-stream",
                    )
                    response.headers["X-Aistudio-Reply-Cache"] = "replay"
                    response.headers["Cache-Control"] = "no-store"
                    return response
                return JSONResponse(
                    completion,
                    headers={
                        "X-Aistudio-Reply-Cache": "replay",
                        "Cache-Control": "no-store",
                    },
                )
            if not await get_reply_cache().enabled():
                owner = None
        except Exception as exc:
            logger.warning("回复缓存读取失败: %s", type(exc).__name__)
            owner = None
    if request.stream:
        bridge = BufferedReplyStream() if owner else None

        async def produce() -> None:
            assert bridge is not None
            await bridge.produce(
                _prepared_openai_events(
                    request, gemini_request, client, cache_owner=owner
                )
            )

        producer = reply_cache_tasks.start(produce) if bridge is not None else None

        async def detached_events() -> AsyncGenerator[str, None]:
            assert bridge is not None and producer is not None
            events = bridge.events(producer)
            try:
                async for chunk in events:
                    yield chunk
            except Exception as exc:
                yield _sse(_late_openai_error(exc))
                yield "data: [DONE]\n\n"
            finally:
                await events.aclose()

        return _OpenAIStreamingResponse(
            _with_keepalive(
                detached_events()
                if producer is not None
                else _prepared_openai_events(
                    request, gemini_request, client, cache_owner=owner
                ),
                ": keepalive\n\n",
            ),
            media_type="text/event-stream",
            on_close=bridge.detach if bridge is not None else None,
        )
    return await _nonstream_openai_response(
        request, gemini_request, client, cache_owner=owner
    )


@router.get("/v1/models")
async def list_openai_models(
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, object]:
    session = runtime_state.client.session if runtime_state.client else None
    from aistudio_api.infrastructure.gateway.model_discovery import model_discovery

    discovered = await model_discovery.get_models(session=session)
    data = []
    seen: set[str] = set()
    for item in discovered:
        model_id = str(item.get("id") or item.get("name") or "").removeprefix("models/")
        ids = [model_id]
        if supports_full_reply(model_id, item):
            ids.append(model_id + ALIAS_SUFFIX)
        for identifier in ids:
            if identifier and identifier not in seen:
                seen.add(identifier)
                data.append(
                    {
                        "id": identifier,
                        "object": "model",
                        "created": 0,
                        "owned_by": "google",
                    }
                )
    return {"object": "list", "data": data}
