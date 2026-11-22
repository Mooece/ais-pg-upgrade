"""Exercise saved-response recovery through the real OpenAI route boundary."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import ClientDisconnect
from starlette.types import Message

from aistudio_api.api import routes_openai as routes
from aistudio_api.api.reply_cache import get_reply_cache
from aistudio_api.api.reply_cache_tasks import ReplyCacheTasks
from aistudio_api.api.schemas import OpenAIChatCompletionRequest
from aistudio_api.infrastructure.gateway.client import AIStudioClient

OWNER = "credential-fingerprint-a"


@pytest.fixture(autouse=True)
async def isolated_background_tasks(monkeypatch):
    manager = ReplyCacheTasks()
    monkeypatch.setattr(routes, "reply_cache_tasks", manager)
    monkeypatch.setattr(routes, "_KEEPALIVE_INTERVAL", 0.01)
    try:
        yield
    finally:
        await manager.shutdown()


def _request(*, stream: bool = False) -> OpenAIChatCompletionRequest:
    return OpenAIChatCompletionRequest(
        model="gemini-test",
        messages=[{"role": "user", "content": "retry the same turn"}],
        stream=stream,
        stream_options={"include_usage": True} if stream else None,
    )


def _client():
    return MagicMock(spec=AIStudioClient)


def _result(text: str = "完整回复") -> dict[str, object]:
    return {
        "candidates": [
            {"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}
        ],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 3},
    }


def _event(payload: dict[str, object]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


async def _read_response(response) -> str:
    if isinstance(response, JSONResponse):
        return bytes(response.body).decode()
    assert isinstance(response, StreamingResponse)
    return "".join(
        [
            chunk.decode() if isinstance(chunk, bytes) else str(chunk)
            async for chunk in response.body_iterator
        ]
    )


async def _wait_for_entry(
    preview: str | None = None, *, exclude_ids: set[str] | None = None
) -> str:
    async with asyncio.timeout(2):
        while True:
            entries = (await get_reply_cache().status())["entries"]
            for entry in entries:
                if entry["id"] in (exclude_ids or set()):
                    continue
                if preview is None or preview in entry["preview"]:
                    return entry["id"]
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_completed_generation_is_saved_before_delivery(monkeypatch, stream):
    cache = get_reply_cache()
    await cache.configure(1)

    async def upstream() -> AsyncGenerator[str, None]:
        yield _event(_result())

    handler = AsyncMock(
        return_value=StreamingResponse(upstream()) if stream else _result()
    )
    monkeypatch.setattr(routes, "handle_gemini_generate_content", handler)
    response = await routes.create_chat_completion(
        _request(stream=stream), _client(), cache_owner=OWNER
    )
    output = await _read_response(response)
    assert "完整回复" in output
    entry_id = await _wait_for_entry()
    await cache.arm(entry_id)
    completion = await cache.take(OWNER)
    assert completion is not None
    choices = completion["choices"]
    assert isinstance(choices, list)
    message = choices[0]["message"]
    assert isinstance(message, dict)
    assert message["content"] == "完整回复"
    usage = completion["usage"]
    assert isinstance(usage, dict)
    assert usage["completion_tokens"] == 3
    assert len((await cache.status())["entries"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_selected_tool_reply_is_replayed_once_without_new_generation(
    monkeypatch, stream
):
    cache = get_reply_cache()
    await cache.configure(2)
    tool_call = {
        "id": "call-original--sig-c2lnbmF0dXJl",
        "type": "function",
        "function": {"name": "reply", "arguments": '{"text":"原始工具回复"}'},
    }
    completion = {
        "id": "chatcmpl-original",
        "object": "chat.completion",
        "created": 1,
        "model": "gemini-test",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tool_call],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": 99,
            "completion_tokens": 88,
            "total_tokens": 187,
            "prompt_tokens_details": {"cached_tokens": 32},
            "completion_tokens_details": {"reasoning_tokens": 44},
        },
    }
    await cache.save(OWNER, completion)
    before = await cache.status()
    entry_id = before["entries"][0]["id"]
    await cache.arm(entry_id)
    handler = AsyncMock(side_effect=HTTPException(429, "synthetic retry"))
    monkeypatch.setattr(routes, "handle_gemini_generate_content", handler)

    response = await routes.create_chat_completion(
        _request(stream=stream), _client(), cache_owner=OWNER
    )
    assert response.headers["X-Aistudio-Reply-Cache"] == "replay"
    assert response.headers["Cache-Control"] == "no-store"
    output = await _read_response(response)
    handler.assert_not_awaited()
    if stream:
        frames = [
            json.loads(frame.removeprefix("data: "))
            for frame in output.strip().split("\n\n")
            if frame != "data: [DONE]"
        ]
        calls = [
            call
            for frame in frames
            for choice in frame["choices"]
            for call in choice["delta"].get("tool_calls", [])
        ]
        assert calls[0]["index"] == 0
        assert calls[0]["id"] == tool_call["id"]
        assert calls[0]["function"] == tool_call["function"]
        assert frames[-1]["usage"]["total_tokens"] == 0
        assert output.endswith("data: [DONE]\n\n")
    else:
        replay = json.loads(output)
        assert replay["id"] != completion["id"]
        assert replay["choices"][0]["message"]["tool_calls"] == [tool_call]
        assert replay["usage"]["total_tokens"] == 0
        assert replay["usage"]["prompt_tokens_details"]["cached_tokens"] == 0
        assert replay["usage"]["completion_tokens_details"]["reasoning_tokens"] == 0
    after = await cache.status()
    assert after["pending_id"] is None
    assert after["entries"] == before["entries"]

    # The reservation was consumed; a second POST must reach the model handler.
    second = routes.create_chat_completion(_request(), _client(), cache_owner=OWNER)
    with pytest.raises(HTTPException):
        await second
    handler.assert_awaited_once()
    await cache.arm(entry_id)
    assert await cache.take(OWNER) == completion


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("phase", ["start", "body"])
@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
async def test_disconnect_still_finishes_and_saves_reply(
    monkeypatch, stream, phase, spec_version
):
    await get_reply_cache().configure(1)
    existing_ids = {
        entry["id"] for entry in (await get_reply_cache().status())["entries"]
    }
    started = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()
    cancelled = False

    async def upstream() -> AsyncGenerator[str, None]:
        nonlocal cancelled
        started.set()
        try:
            await release.wait()
            yield _event(_result("断线后完整生成"))
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            closed.set()

    async def handle(*args, **kwargs):
        nonlocal cancelled
        if stream:
            return StreamingResponse(upstream())
        started.set()
        try:
            await release.wait()
            return _result("断线后完整生成")
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            closed.set()

    monkeypatch.setattr(routes, "handle_gemini_generate_content", handle)
    response = await routes.create_chat_completion(
        _request(stream=stream), _client(), cache_owner=OWNER
    )

    async def receive() -> Message:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response." + phase:
            raise OSError("synthetic disconnected subscriber")

    scope = {"type": "http", "asgi": {"spec_version": spec_version}}
    with pytest.raises((ClientDisconnect, OSError)):
        await response(scope, receive, send)
    await asyncio.wait_for(started.wait(), 1)
    assert not closed.is_set()
    assert not cancelled
    release.set()
    entry_id = await _wait_for_entry("断线后完整生成", exclude_ids=existing_ids)
    assert closed.is_set() and not cancelled
    cache = get_reply_cache()
    await cache.arm(entry_id)
    completion = await cache.take(OWNER)
    assert completion is not None
    choices = completion["choices"]
    assert isinstance(choices, list)
    message = choices[0]["message"]
    assert isinstance(message, dict)
    assert message["content"] == "断线后完整生成"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error_event", "truncated", "exception"])
async def test_failed_or_unfinished_stream_does_not_replace_last_good_reply(
    monkeypatch, failure
):
    cache = get_reply_cache()
    await cache.configure(1)
    await cache.save(
        OWNER, {"model": "previous", "choices": [{"message": {"content": "good"}}]}
    )
    before = (await cache.status())["entries"]

    async def upstream() -> AsyncGenerator[str, None]:
        yield _event({"candidates": [{"content": {"parts": [{"text": "partial"}]}}]})
        if failure == "error_event":
            yield _event(
                {"error": {"code": 502, "message": "synthetic upstream failure"}}
            )
        elif failure == "exception":
            raise OSError("synthetic upstream reset")

    monkeypatch.setattr(
        routes,
        "handle_gemini_generate_content",
        AsyncMock(return_value=StreamingResponse(upstream())),
    )
    response = await routes.create_chat_completion(
        _request(stream=True), _client(), cache_owner=OWNER
    )
    await _read_response(response)
    assert (await cache.status())["entries"] == before


@pytest.mark.asyncio
async def test_wrong_credential_cannot_consume_pending_replay(monkeypatch):
    cache = get_reply_cache()
    await cache.configure(1)
    await cache.save(
        OWNER, {"model": "original", "choices": [{"message": {"content": "private"}}]}
    )
    entry_id = (await cache.status())["entries"][0]["id"]
    await cache.arm(entry_id)
    handler = AsyncMock(side_effect=HTTPException(429, "synthetic failure"))
    monkeypatch.setattr(routes, "handle_gemini_generate_content", handler)
    with pytest.raises(HTTPException):
        await routes.create_chat_completion(
            _request(), _client(), cache_owner="other-key"
        )
    handler.assert_awaited_once()
    assert (await cache.status())["pending_id"] == entry_id
    assert await cache.take(OWNER) is not None
