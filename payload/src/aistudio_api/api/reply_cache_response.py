"""Conversion helpers for storing and replaying OpenAI completions."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator


class ReplyCapture:
    def __init__(self) -> None:
        self._content: list[str] = []
        self._tool_calls: dict[int, dict[str, object]] = {}

    def add_delta(self, delta: dict[str, object]) -> None:
        content = delta.get("content")
        if isinstance(content, str):
            self._content.append(content)
        calls = delta.get("tool_calls")
        if isinstance(calls, list):
            for raw in calls:
                if not isinstance(raw, dict):
                    continue
                index = raw.get("index")
                if not isinstance(index, int):
                    continue
                current = self._tool_calls.setdefault(
                    index, {"type": "function", "function": {}}
                )
                if raw.get("id"):
                    current["id"] = raw["id"]
                function = current["function"]
                incoming = raw.get("function")
                if isinstance(function, dict) and isinstance(incoming, dict):
                    if incoming.get("name"):
                        function["name"] = incoming["name"]
                    if incoming.get("arguments"):
                        function["arguments"] = str(
                            function.get("arguments", "")
                        ) + str(incoming["arguments"])

    def complete(
        self,
        *,
        completion_id: str,
        created: int,
        model: str,
        finish_reason: str,
        usage: dict[str, object],
        full_reply: dict[str, object] | None = None,
    ) -> dict[str, object]:
        message: dict[str, object] = {
            "role": "assistant",
            "content": "".join(self._content) or None,
        }
        if self._tool_calls:
            message["tool_calls"] = [
                self._tool_calls[i] for i in sorted(self._tool_calls)
            ]
        result: dict[str, object] = {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {"index": 0, "message": message, "finish_reason": finish_reason}
            ],
            "usage": usage,
        }
        if full_reply is not None:
            result["aistudio_full_reply"] = full_reply
        return result


def replay_completion(completion: dict[str, object]) -> dict[str, object]:
    result = dict(completion)
    result["id"] = f"chatcmpl-replay-{int(time.time() * 1000)}"
    raw_usage = result.get("usage")
    usage: dict[str, object] = (
        {key: value for key, value in raw_usage.items() if isinstance(key, str)}
        if isinstance(raw_usage, dict)
        else {}
    )
    usage.update({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    for detail_key in ("prompt_tokens_details", "completion_tokens_details"):
        details = usage.get(detail_key)
        if isinstance(details, dict):
            usage[detail_key] = {
                key: 0 if key.endswith("_tokens") else value
                for key, value in details.items()
                if isinstance(key, str)
            }
    result["usage"] = usage
    return result


def _sse(payload: dict[str, object]) -> str:
    return (
        "data: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    )


async def replay_chunks(
    completion: dict[str, object], *, include_usage: bool
) -> AsyncGenerator[str, None]:
    model = str(completion.get("model", "unknown"))
    response_id = str(completion.get("id", "chatcmpl-replay"))
    raw_created = completion.get("created")
    try:
        created = (
            int(raw_created)
            if isinstance(raw_created, (int, float, str)) and raw_created
            else int(time.time())
        )
    except (OverflowError, ValueError):
        created = int(time.time())
    choices = completion.get("choices")
    raw_choice = choices[0] if isinstance(choices, list) and choices else {}
    choice = raw_choice if isinstance(raw_choice, dict) else {}
    raw_message = choice.get("message")
    message = raw_message if isinstance(raw_message, dict) else {}
    yield _sse(
        {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
            ],
        }
    )
    delta = {}
    if message.get("content") is not None:
        delta["content"] = message["content"]
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        delta["tool_calls"] = [
            {**call, "index": index}
            for index, call in enumerate(tool_calls)
            if isinstance(call, dict)
        ]
    if delta:
        full_reply = completion.get("aistudio_full_reply")
        yield _sse(
            {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                **(
                    {"aistudio_full_reply": full_reply}
                    if isinstance(full_reply, dict)
                    else {}
                ),
            }
        )
    finish = choice.get("finish_reason") if isinstance(raw_choice, dict) else "stop"
    yield _sse(
        {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
        }
    )
    if include_usage:
        raw_usage = completion.get("usage")
        yield _sse(
            {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [],
                "usage": raw_usage
                if isinstance(raw_usage, dict)
                else {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            }
        )
    yield "data: [DONE]\n\n"
