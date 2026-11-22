"""Cancellation cleanup retains upstream pooled-client lifecycle guarantees."""

from __future__ import annotations

import json
import logging

import anyio
import httpx
import pytest

from aistudio_api.infrastructure.gateway.transport import HTTPStreamTransport
from aistudio_api.infrastructure.utils.request_diagnostics import request_diagnostic_id


@pytest.mark.asyncio
async def test_cancelled_stream_closes_response_and_drained_stale_pool(caplog):
    caplog.set_level(logging.INFO, logger="aistudio.request_diagnostics")
    response_closed = anyio.Event()
    pool_closed = anyio.Event()

    class Source(httpx.AsyncByteStream):
        async def __aiter__(self):
            await anyio.sleep_forever()
            yield b"unreachable"

        async def aclose(self):
            await anyio.sleep(0)
            response_closed.set()

    class ClosingTransport(httpx.MockTransport):
        async def aclose(self):
            await anyio.sleep(0)
            pool_closed.set()

    def handler(request):
        return httpx.Response(200, stream=Source())

    transport = HTTPStreamTransport()
    client = httpx.AsyncClient(transport=ClosingTransport(handler))
    transport._client = client
    token = request_diagnostic_id.set("synthetic-stale-pool-cancel")
    iterator = transport.send_streaming_request(
        url="https://example.invalid", headers={}, body="{}", cookies={}
    )
    try:
        with anyio.CancelScope() as scope:
            assert await anext(iterator) == ("status", 200)
            # A concurrent request detached this pool after a connection failure.
            # This stream is the last in-flight user, so its cleanup must close it.
            transport._client = None
            transport._stale_clients[id(client)] = client
            scope.cancel()
            await anext(iterator)
        assert response_closed.is_set()
        assert pool_closed.is_set()
        assert not transport._inflight
        assert not transport._stale_clients
    finally:
        await iterator.aclose()
        await client.aclose()
        request_diagnostic_id.reset(token)

    events = [
        json.loads(record.getMessage().removeprefix("request_lifecycle "))
        for record in caplog.records
        if record.name == "aistudio.request_diagnostics"
    ]
    end = next(event for event in events if event["event"] == "upstream_end")
    assert end["reason"] == "cancelled"
    assert end["received_bytes"] == 0


@pytest.mark.asyncio
async def test_early_iterator_close_logs_received_bytes_without_payload(caplog):
    caplog.set_level(logging.INFO, logger="aistudio.request_diagnostics")

    class Source(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"PRIVATE-PROMPT"
            yield b"unread"

    def handler(request):
        return httpx.Response(200, stream=Source())

    transport = HTTPStreamTransport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport._client = client
        token = request_diagnostic_id.set("synthetic-iterator-close")
        iterator = transport.send_streaming_request(
            url="https://example.invalid", headers={}, body="{}", cookies={}
        )
        try:
            assert await anext(iterator) == ("status", 200)
            assert await anext(iterator) == ("chunk", b"PRIVATE-PROMPT")
            await iterator.aclose()
            assert not transport._inflight
        finally:
            await iterator.aclose()
            request_diagnostic_id.reset(token)

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "aistudio.request_diagnostics"
    ]
    assert all("PRIVATE" not in message for message in messages)
    events = [
        json.loads(message.removeprefix("request_lifecycle ")) for message in messages
    ]
    end = next(event for event in events if event["event"] == "upstream_end")
    assert end["reason"] == "iterator_closed"
    assert end["received_bytes"] == len(b"PRIVATE-PROMPT")
    assert end["received_chunks"] == 1
