"""Lifecycle logs contain metadata and survive stream cancellation cleanup."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from aistudio_api.infrastructure.gateway.transport import HTTPStreamTransport
from aistudio_api.infrastructure.utils.request_diagnostics import (
    log_request_event,
    request_diagnostic_id,
)


def test_diagnostics_require_context_and_drop_payload_fields(caplog):
    caplog.set_level(logging.INFO, logger="aistudio.request_diagnostics")
    log_request_event("not_observed", sent_bytes=5)
    assert not caplog.records
    token = request_diagnostic_id.set("synthetic-request")
    try:
        log_request_event(
            "request_start",
            sent_bytes=5,
            exception_type="ConnectionResetError",
            body="PRIVATE-PROMPT",
            authorization="PRIVATE-KEY",
            message="PRIVATE-EXCEPTION",
        )
    finally:
        request_diagnostic_id.reset(token)
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    payload = json.loads(message.removeprefix("request_lifecycle "))
    assert payload["request_id"] == "synthetic-request"
    assert payload["sent_bytes"] == 5
    assert payload["python_version"]
    assert "PRIVATE" not in message


@pytest.mark.asyncio
async def test_transport_cancel_logs_reason_and_awaits_response_close(caplog):
    caplog.set_level(logging.INFO, logger="aistudio.request_diagnostics")
    pending = asyncio.Event()
    started = asyncio.Event()
    closed = asyncio.Event()

    class Source(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await pending.wait()
            yield b"unreachable"

        async def aclose(self):
            await asyncio.sleep(0)
            closed.set()

    async def handler(request):
        return httpx.Response(200, stream=Source())

    transport = HTTPStreamTransport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport._client = client
        token = request_diagnostic_id.set("synthetic-cancel")
        try:
            iterator = transport.send_streaming_request(
                url="https://example.invalid", headers={}, body="{}", cookies={}
            )
            assert await anext(iterator) == ("status", 200)
            task = asyncio.create_task(anext(iterator))
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert closed.is_set()
            await iterator.aclose()
        finally:
            request_diagnostic_id.reset(token)
    events = [
        json.loads(record.getMessage().removeprefix("request_lifecycle "))
        for record in caplog.records
        if record.name == "aistudio.request_diagnostics"
    ]
    assert any(event["event"] == "upstream_cancelled" for event in events)
    end = next(event for event in events if event["event"] == "upstream_end")
    assert end["reason"] == "cancelled"
    assert end["received_bytes"] == 0
