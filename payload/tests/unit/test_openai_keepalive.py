"""Exercise idle OpenAI responses without real accounts or model requests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import ClientDisconnect

from aistudio_api.api import routes_openai as routes
from aistudio_api.api.schemas import OpenAIChatCompletionRequest
from aistudio_api.infrastructure.gateway.client import AIStudioClient


@pytest.fixture(autouse=True)
def short_intervals(monkeypatch):
    monkeypatch.setattr(routes, "_KEEPALIVE_INTERVAL", 0.01)


def request(*, stream=False):
    return OpenAIChatCompletionRequest(
        model="gemini-test", messages=[{"role": "user", "content": "hi"}], stream=stream
    )


def result():
    return {"candidates": [{"content": {"parts": [{"text": "final"}]}}]}


def client():
    return MagicMock(spec=AIStudioClient)


@pytest.mark.asyncio
async def test_thought_only_activity_does_not_suppress_final_sse_heartbeat():
    release = asyncio.Event()
    closed = asyncio.Event()
    reads = 0

    async def upstream():
        nonlocal reads
        try:
            reads += 1
            yield 'data: {"candidates":[{"content":{"parts":[{"thought":true,"text":"private"}]}}]}\n\n'
            reads += 1
            await release.wait()
            yield 'data: {"candidates":[{"content":{"parts":[{"text":"final"}]}}]}\n\n'
        finally:
            closed.set()

    events = routes._openai_stream(
        StreamingResponse(upstream()), model="gemini-test", include_usage=False
    )
    try:
        assert '"role":"assistant"' in await anext(events)
        assert await anext(events) == ": keepalive\n\n"
        assert await anext(events) == ": keepalive\n\n"
        # Both heartbeats await the same read; neither closes/restarts upstream.
        assert reads == 2
        assert not closed.is_set()
        release.set()
        chunks = [chunk async for chunk in events]
        assert "private" not in "".join(chunks)
        assert '"content":"final"' in "".join(chunks)
        assert chunks[-1] == "data: [DONE]\n\n"
    finally:
        await events.aclose()
    assert closed.is_set()


@pytest.mark.asyncio
async def test_closing_sse_while_idle_awaits_pending_read_cleanup():
    closed = asyncio.Event()
    blocked = asyncio.Event()

    async def upstream():
        try:
            await blocked.wait()
            yield "unreachable"
        finally:
            await asyncio.sleep(0)
            closed.set()

    events = routes._openai_stream(
        StreamingResponse(upstream()), model="gemini-test", include_usage=False
    )
    await anext(events)
    assert await anext(events) == ": keepalive\n\n"
    await events.aclose()
    assert closed.is_set()


@pytest.mark.asyncio
async def test_result_at_keepalive_boundary_is_not_lost(monkeypatch):
    original_wait = asyncio.wait
    triggered = False

    async def boundary_wait(tasks, **kwargs):
        nonlocal triggered
        if not triggered:
            triggered = True
            await original_wait(tasks)
            # A timeout can race completion before the caller examines done.
            return set(), set(tasks)
        return await original_wait(tasks, **kwargs)

    monkeypatch.setattr(routes.asyncio, "wait", boundary_wait)

    async def source():
        yield "complete"

    assert [chunk async for chunk in routes._with_keepalive(source(), "heartbeat")] == [
        "heartbeat",
        "complete",
    ]


@pytest.mark.asyncio
async def test_nonstream_fast_error_keeps_http_status(monkeypatch):
    handler = AsyncMock(side_effect=HTTPException(429, detail={"message": "limited"}))
    monkeypatch.setattr(routes, "handle_gemini_generate_content", handler)
    req = request()
    with pytest.raises(HTTPException) as caught:
        await routes.create_chat_completion(req, client())
    assert caught.value.status_code == 429


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_nonstream_preparation_delay_sends_json_whitespace_then_final(
    monkeypatch, failure
):
    release = asyncio.Event()

    async def handle(*args, **kwargs):
        await release.wait()
        if failure:
            raise HTTPException(429, detail={"message": "limited"})
        return result()

    monkeypatch.setattr(routes, "handle_gemini_generate_content", handle)
    response = await routes.create_chat_completion(request(), client())
    assert isinstance(response, StreamingResponse)
    assert response.status_code == 200
    assert response.media_type == "application/json"
    iterator = response.body_iterator
    assert isinstance(iterator, AsyncGenerator)
    try:
        assert await anext(iterator) == b"\n"
        assert await anext(iterator) == b"\n"
        release.set()
        payload = json.loads(
            "".join(
                [
                    chunk.decode() if isinstance(chunk, bytes) else str(chunk)
                    async for chunk in iterator
                ]
            )
        )
        if failure:
            assert payload["error"]["code"] == 429
            assert payload["error"]["type"] == "rate_limit_error"
        else:
            assert payload["choices"][0]["message"]["content"] == "final"
    finally:
        await iterator.aclose()


@pytest.mark.asyncio
async def test_nonstream_inner_whitespace_does_not_end_outer_keepalive(monkeypatch):
    release = asyncio.Event()
    closed = asyncio.Event()

    async def upstream():
        try:
            yield b"\n"
            await release.wait()
            yield b'{"error":{"code":403,"message":"denied"}}'
        finally:
            closed.set()

    monkeypatch.setattr(
        routes,
        "handle_gemini_generate_content",
        AsyncMock(return_value=StreamingResponse(upstream())),
    )
    response = await routes.create_chat_completion(request(), client())
    assert isinstance(response, StreamingResponse)
    iterator = response.body_iterator
    assert isinstance(iterator, AsyncGenerator)
    try:
        assert await anext(iterator) == b"\n"
        assert await anext(iterator) == b"\n"
        release.set()
        payload = json.loads(
            "".join(
                [
                    chunk.decode() if isinstance(chunk, bytes) else str(chunk)
                    async for chunk in iterator
                ]
            )
        )
        assert payload["error"]["code"] == 403
        assert payload["error"]["type"] == "permission_error"
    finally:
        await iterator.aclose()
    assert closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["start", "body"])
@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
async def test_nonstream_disconnect_cleans_result_even_before_first_body(
    monkeypatch, phase, spec_version
):
    closed = asyncio.Event()
    blocked = asyncio.Event()

    async def handle(*args, **kwargs):
        try:
            await blocked.wait()
        finally:
            await asyncio.sleep(0)
            closed.set()

    monkeypatch.setattr(routes, "handle_gemini_generate_content", handle)
    response = await routes.create_chat_completion(request(), client())

    async def receive():
        await blocked.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response." + phase:
            raise OSError("synthetic disconnect")

    scope = {"type": "http", "asgi": {"spec_version": spec_version}}
    with pytest.raises((ClientDisconnect, OSError)):
        await response(scope, receive, send)
    assert closed.is_set()


@pytest.mark.asyncio
async def test_stream_preparation_is_covered_by_heartbeat(monkeypatch):
    closed = asyncio.Event()
    blocked = asyncio.Event()

    async def handle(*args, **kwargs):
        try:
            await blocked.wait()
        finally:
            closed.set()

    monkeypatch.setattr(routes, "handle_gemini_generate_content", handle)
    response = await routes.create_chat_completion(request(stream=True), client())
    assert isinstance(response, StreamingResponse)
    iterator = response.body_iterator
    assert isinstance(iterator, AsyncGenerator)
    assert await anext(iterator) == ": keepalive\n\n"
    await iterator.aclose()
    assert closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
async def test_stream_response_task_cancellation_waits_for_upstream_cleanup(
    monkeypatch, spec_version
):
    started = asyncio.Event()
    closed = asyncio.Event()
    pending = asyncio.Event()

    async def upstream():
        try:
            started.set()
            await pending.wait()
            yield "unreachable"
        finally:
            await asyncio.sleep(0)
            closed.set()

    monkeypatch.setattr(
        routes,
        "handle_gemini_generate_content",
        AsyncMock(return_value=StreamingResponse(upstream())),
    )
    response = await routes.create_chat_completion(request(stream=True), client())

    async def receive():
        await pending.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        return None

    scope = {"type": "http", "asgi": {"spec_version": spec_version}}
    task = asyncio.create_task(response(scope, receive, send))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stream_error_preserves_code_and_done_marker():
    async def upstream():
        yield 'data: {"error":{"code":403,"message":"denied"}}\n\n'

    chunks = [
        chunk
        async for chunk in routes._openai_stream(
            StreamingResponse(upstream()), model="gemini-test", include_usage=False
        )
    ]
    payload = json.loads(chunks[1].removeprefix("data: "))
    assert payload["error"]["code"] == 403
    assert payload["error"]["type"] == "permission_error"
    assert chunks[-1] == "data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_nonstream_fast_success_returns_normal_json(monkeypatch):
    monkeypatch.setattr(
        routes, "handle_gemini_generate_content", AsyncMock(return_value=result())
    )
    response = await routes.create_chat_completion(request(), client())
    assert isinstance(response, JSONResponse)
    assert response.status_code == 200
    assert (
        json.loads(bytes(response.body))["choices"][0]["message"]["content"] == "final"
    )
