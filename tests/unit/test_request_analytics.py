"""Client request metadata remains accurate across streams, retries and restarts."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect

from aistudio_api.api import analytics, analytics_middleware
from aistudio_api.api.analytics import (
    AnalyticsStore,
    RequestRecord,
    current_request,
    flush_analytics,
    period_bounds,
    record_request_usage,
)
from aistudio_api.api.analytics_middleware import (
    RequestAnalyticsMiddleware,
    _RequestMetadata,
    _StreamObserver,
)
from aistudio_api.api.state import RuntimeState
from aistudio_api.infrastructure.utils.request_diagnostics import request_diagnostic_id


def _query(store, *args, **kwargs):
    # Exercise the JSON contract consumers receive rather than an opaque nested
    # object dictionary. Response.json uses the same decoding as route tests.
    return httpx.Response(200, json=store.query(*args, **kwargs)).json()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "late_error", "incomplete", "sse_error"])
async def test_large_generation_outcomes_are_observed_without_response_buffering(
    tmp_path, monkeypatch, mode
):
    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(tmp_path))
    app = FastAPI()
    app.add_middleware(RequestAnalyticsMiddleware)
    huge_text = "x" * (150 * 1024)
    payload = json.dumps(
        {"choices": [{"message": {"content": huge_text}}]}
        if mode == "success"
        else {"error": {"message": huge_text, "code": 429}}
    ).encode()
    if mode == "incomplete":
        payload = payload[:-10]
    if mode == "sse_error":
        payload = b"data: " + payload + b"\n\n"

    @app.post("/v1/chat/completions")
    async def endpoint(request: Request):
        await request.json()

        async def chunks():
            if mode != "sse_error":
                yield b" \n "
            for index in range(0, len(payload), 997):
                yield payload[index : index + 997]

        return StreamingResponse(
            chunks(),
            media_type="text/event-stream"
            if mode == "sse_error"
            else "application/json",
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/v1/chat/completions", json={"model": "test"})
    assert response.status_code == 200
    row = _query(analytics.get_analytics_store())["requests"]["items"][0]
    assert row["status"] == (
        "success"
        if mode == "success"
        else "error"
        if mode == "incomplete"
        else "rate_limited"
    )
    assert row["status_code"] == (
        200 if mode == "success" else 502 if mode == "incomplete" else 429
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,termination,error_type",
    [
        ("normal", "normal_complete", None),
        ("incomplete", "incomplete_return", None),
        ("disconnect", "http_disconnect", None),
        ("cancel", "cancelled_error", asyncio.CancelledError),
        ("connection", "connection_error", ConnectionError),
        ("client", "client_disconnect", ClientDisconnect),
        ("send", "send_failed", OSError),
    ],
)
async def test_request_diagnostics_distinguish_termination_and_reset_context(
    tmp_path, monkeypatch, mode, termination, error_type
):
    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(tmp_path))
    events = []

    def capture(event, **fields):
        events.append(
            {"event": event, "request_id": request_diagnostic_id.get(), **fields}
        )

    monkeypatch.setattr(analytics_middleware, "log_request_event", capture)
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b'{"model":"test","prompt":"PRIVATE"}'}

    async def send(message):
        if mode == "send" and message["type"] == "http.response.body":
            raise OSError(32, "PRIVATE ERROR")

    async def endpoint(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        if mode in ("cancel", "connection", "client"):
            assert error_type is not None
            raise error_type("PRIVATE ERROR")
        if mode == "disconnect":
            await receive()
        await send(
            {
                "type": "http.response.body",
                "body": b"ok",
                "more_body": mode == "incomplete",
            }
        )

    middleware = RequestAnalyticsMiddleware(endpoint)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "asgi": {"spec_version": "2.4"},
    }
    if error_type is not None:
        with pytest.raises(error_type):
            await middleware(scope, receive, send)
    else:
        await middleware(scope, receive, send)
    row = _query(analytics.get_analytics_store())["requests"]["items"][0]
    final = events[-1]
    assert final["event"] == "request_end"
    assert final["termination"] == termination
    assert final["sent_bytes"] == (0 if error_type is not None else 2)
    assert final["http_disconnect_observed"] == (mode == "disconnect")
    assert final["delivery_confirmation"] == "asgi_send_completed_only"
    assert all(event["request_id"] == row["id"] for event in events)
    assert "PRIVATE" not in json.dumps(events)
    assert request_diagnostic_id.get() is None
    assert current_request.get() is None


def _completed(now=None, **changes):
    return RequestRecord(
        started_at=(now or datetime.now(UTC)).isoformat(),
        status="success",
        status_code=200,
        duration_ms=100,
        prompt_tokens=20,
        completion_tokens=5,
        total_tokens=25,
        usage_known=True,
        **changes,
    )


def test_persistent_aggregation_filters_and_paging(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics.sqlite3")
    now = datetime.now(UTC)
    records = [
        replace(
            _completed(
                now - timedelta(minutes=3), model="one", stream=True, ttft_ms=40
            ),
            duration_ms=200,
        ),
        replace(
            _completed(now - timedelta(minutes=2), model="one"),
            status="rate_limited",
            duration_ms=300,
            ttft_ms=50,
        ),
        replace(
            _completed(now - timedelta(minutes=1), model="two"),
            status="error",
            duration_ms=400,
        ),
    ]
    for record in records:
        store.complete(record)
    live = RequestRecord(model="one")
    store.begin(live)
    result = _query(store, now=now, page_size=2)
    assert result["totals"]["requests"] == 4
    assert result["totals"]["avg_duration_ms"] == 300
    assert result["totals"]["avg_ttft_ms"] == 40
    assert result["totals"]["p95_duration_ms"] == 400
    assert result["totals"]["success_rate"] == 33.33
    assert result["totals"]["active_requests"] == 1
    assert len(result["timeseries"]) == 7
    assert sum(item["requests"] for item in result["timeseries"]) == 4
    assert {item["name"]: item["requests"] for item in result["models"]} == {
        "one": 3,
        "two": 1,
    }
    assert all("timeseries" not in item for item in result["models"])
    first = result["requests"]["items"]
    second = _query(store, now=now, page_size=2, page=2)["requests"]["items"]
    assert [item["id"] for item in first + second] == [
        live.id,
        *[record.id for record in reversed(records)],
    ]
    assert _query(store, model="one")["totals"]["requests"] == 3
    assert (
        _query(store, status="rate_limited")["requests"]["items"][0]["id"]
        == records[1].id
    )
    assert _query(store, status="active")["requests"]["items"][0]["id"] == live.id
    restored = _query(AnalyticsStore(store.path))
    assert restored["totals"]["requests"] == 3
    assert restored["filters"]["models"] == ["one", "two"]
    assert restored["history_started_at"]
    assert restored["requests"]["items"][0]["usage_known"] is True


def test_calendar_boundaries_timezone_and_zero_buckets(tmp_path):
    now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    start, end = period_bounds("today", 480, now)
    store = AnalyticsStore(tmp_path / "analytics.sqlite3")
    for date in (
        start - timedelta(microseconds=1),
        start,
        end - timedelta(microseconds=1),
        end,
    ):
        store.complete(_completed(date))
    result = _query(store, "today", "hour", now=now)
    assert result["totals"]["requests"] == 2
    assert len(result["timeseries"]) == 24
    assert result["timeseries"][0]["label"].endswith("00:00")
    assert result["timeseries"][0]["requests"] == 1
    assert result["timeseries"][-1]["requests"] == 1
    january = datetime(2026, 1, 1, 12, tzinfo=UTC)
    last_start, last_end = period_bounds("last_month", -480, january)
    assert last_start == datetime(2025, 12, 1, 8, tzinfo=UTC)
    assert last_end == datetime(2026, 1, 1, 8, tzinfo=UTC)


def test_old_active_requests_merge_without_duplicates_on_later_pages(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics.sqlite3")
    now = datetime.now(UTC)
    records = [_completed(now - timedelta(minutes=index * 2)) for index in range(8)]
    for record in records:
        store.complete(record)
    live = [
        RequestRecord(started_at=(now - timedelta(minutes=minute)).isoformat())
        for minute in (1, 9, 20)
    ]
    for record in live:
        store.begin(record)
    items = [
        item
        for page in range(1, 5)
        for item in _query(store, page=page, page_size=3)["requests"]["items"]
    ]
    expected = sorted(
        records + live, key=lambda record: record.started_at, reverse=True
    )
    assert [item["id"] for item in items] == [record.id for record in expected]
    assert _query(store, page=100, page_size=3)["requests"]["items"] == []


def test_retention_and_capacity_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(analytics, "MAX_RECORDS", 3)
    store = AnalyticsStore(tmp_path / "analytics.sqlite3")
    old = _completed(datetime.now(UTC) - timedelta(days=91))
    store.complete(old)
    for minutes in range(5):
        store.complete(_completed(datetime.now(UTC) - timedelta(minutes=minutes)))
    with closing(sqlite3.connect(store.path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 3
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM requests WHERE id=?", (old.id,)
            ).fetchone()[0]
            == 0
        )


def test_metadata_lexer_does_not_capture_prompt_or_nested_fields():
    record = RequestRecord()
    scanner = _RequestMetadata(record)
    payload = json.dumps(
        {
            "messages": [{"content": "SECRET" * 20000, "model": "wrong"}],
            "model": "models/gemini-test",
            "stream": True,
        }
    ).encode()
    for index in range(0, len(payload), 7):
        scanner.feed(payload[index : index + 7])
        assert len(scanner.capture) <= 256
    assert record.model == "gemini-test"
    assert record.stream is True
    assert "SECRET" not in repr(scanner.__dict__)


def test_ttft_ignores_role_usage_heartbeat_and_signatures():
    record = RequestRecord(stream=True)
    observer = _StreamObserver(record)
    for data in (
        b": keepalive\n\n",
        b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
        b'data: {"usage":{"total_tokens":50},"choices":[]}\n\n',
        b'data: {"candidates":[{"content":{"parts":[{"thoughtSignature":"sig"}]}}]}\n\n',
    ):
        for byte in data:
            observer.feed(bytes([byte]))
        assert record.ttft_ms is None
    data = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
    for byte in data:
        observer.feed(bytes([byte]))
    assert record.ttft_ms is not None
    first = record.ttft_ms
    observer.feed(
        b'data: {"candidates":[{"content":{"parts":[{"text":"later"}]}}]}\n\n'
    )
    assert record.ttft_ms == first


@pytest.mark.parametrize(
    "prefix",
    [
        b'data: {"candidates":[{"content":{"parts":[{"inlineData":{"data":"',
        b'data: {"choices":[{"delta":{"content":"',
        b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"arguments":"',
    ],
)
def test_large_content_frame_is_bounded_and_observed(prefix):
    record = RequestRecord(stream=True)
    observer = _StreamObserver(record)
    observer.feed(prefix + b"x" * 200000)
    assert len(observer.pending) <= observer.MAX_FRAME_BYTES
    observer.feed(b'"}}]}}]}\n\n')
    assert record.ttft_ms is not None
    assert observer.pending == b""


@pytest.mark.asyncio
async def test_concurrent_requests_retry_hook_and_legacy_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("aistudio_api.api.state.settings.persist_stats", False)
    state = RuntimeState()
    app = FastAPI()
    app.add_middleware(RequestAnalyticsMiddleware)

    @app.post("/v1/chat/completions")
    async def endpoint(request: Request):
        payload = await request.json()
        # A retry's 429 must not become a second client request row.
        state.record(payload["model"], "rate_limited")
        await asyncio.sleep(0)
        state.record(
            payload["model"],
            "success",
            {"prompt_tokens": payload["tokens"], "completion_tokens": 2},
            "STOP",
        )
        return {"choices": []}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await asyncio.gather(
            *(
                client.post(
                    "/v1/chat/completions",
                    json={"model": f"test-{number}", "tokens": number + 1},
                )
                for number in range(12)
            )
        )
    result = _query(analytics.get_analytics_store())
    assert result["totals"]["requests"] == 12
    assert result["totals"]["success"] == 12
    assert result["totals"]["rate_limited"] == 0
    assert result["totals"]["prompt_tokens"] == sum(range(1, 13))
    for record in result["requests"]["items"]:
        number = int(record["model"].split("-")[-1])
        assert record["prompt_tokens"] == number + 1
        assert state.model_stats[record["model"]].requests == 2
        assert state.model_stats[record["model"]].rate_limited == 1
    assert current_request.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,status,code",
    [
        ({"code": 400, "message": "private error"}, "error", 400),
        ({"type": "rate_limit_error", "message": "private error"}, "rate_limited", 429),
    ],
)
async def test_http200_sse_errors_and_full_stream_duration(
    tmp_path, monkeypatch, error, status, code
):
    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(tmp_path))
    app = FastAPI()
    app.add_middleware(RequestAnalyticsMiddleware)

    @app.post("/v1beta/models/test:streamGenerateContent")
    async def endpoint():
        async def events():
            yield ": ping\n\n"
            await asyncio.sleep(0.015)
            yield 'data: {"candidates":[{"content":{"parts":[{"text":"hello"}]}}]}\n\n'
            await asyncio.sleep(0.015)
            yield "data: " + json.dumps({"error": error}) + "\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1beta/models/test:streamGenerateContent", json={}
        )
    assert response.status_code == 200
    result = _query(analytics.get_analytics_store())
    row = result["requests"]["items"][0]
    assert row["status"] == status
    assert row["status_code"] == code
    assert row["duration_ms"] >= 25
    assert row["ttft_ms"] >= 10
    assert row["ttft_ms"] < row["duration_ms"]
    assert row["usage_known"] is False
    assert result["totals"]["avg_ttft_ms"] is None
    assert (
        "private error"
        not in analytics.get_analytics_store().path.read_bytes().decode(errors="ignore")
    )


@pytest.mark.asyncio
async def test_stream_cancelled_on_task_cancellation_and_disconnect(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(tmp_path))
    messages = []

    async def receive():
        return {"type": "http.request", "body": b'{"model":"test"}', "more_body": False}

    async def send(message):
        messages.append(message)

    async def cancelled_app(scope, receive, send):
        await receive()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        raise asyncio.CancelledError

    scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions"}
    with pytest.raises(asyncio.CancelledError):
        await RequestAnalyticsMiddleware(cancelled_app)(scope, receive, send)
    await flush_analytics()
    result = _query(analytics.get_analytics_store())
    assert result["requests"]["items"][0]["status"] == "cancelled"
    assert result["totals"]["active_requests"] == 0

    async def disconnect_receive():
        return {"type": "http.disconnect"}

    async def disconnect_app(scope, receive, send):
        await receive()

    await RequestAnalyticsMiddleware(disconnect_app)(scope, disconnect_receive, send)
    assert _query(analytics.get_analytics_store())["totals"]["cancelled"] == 2


@pytest.mark.asyncio
async def test_starlette_client_disconnect_is_cancelled(tmp_path, monkeypatch):
    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(tmp_path))

    async def endpoint(scope, receive, send):
        async def events():
            yield 'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'

        await StreamingResponse(events(), media_type="text/event-stream")(
            scope, receive, send
        )

    async def receive():
        return {"type": "http.request", "body": b"{}"}

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("connection lost")

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "asgi": {"spec_version": "2.4"},
    }
    with pytest.raises(ClientDisconnect):
        await RequestAnalyticsMiddleware(endpoint)(scope, receive, send)
    result = _query(analytics.get_analytics_store())
    assert result["requests"]["items"][0]["status"] == "cancelled"
    assert result["totals"]["errors"] == 0


@pytest.mark.asyncio
async def test_stream_usage_context_survives_base_http_middleware(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(tmp_path))
    app = FastAPI()

    @app.middleware("http")
    async def existing_middleware(request, call_next):
        return await call_next(request)

    app.add_middleware(RequestAnalyticsMiddleware)

    @app.post("/v1/chat/completions")
    async def endpoint(request: Request):
        await request.json()

        async def events():
            yield 'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
            yield 'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            record_request_usage(
                "models/test",
                "success",
                {"prompt_tokens": 42, "completion_tokens": 8},
                "STOP",
            )
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions", json={"model": "test", "stream": True}
        )
    assert response.status_code == 200
    row = _query(analytics.get_analytics_store())["requests"]["items"][0]
    assert row["status"] == "success"
    assert row["usage_known"] is True
    assert row["total_tokens"] == 50
    assert row["ttft_ms"] is not None


@pytest.mark.asyncio
async def test_statistics_failure_does_not_break_response(tmp_path, monkeypatch):
    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(tmp_path))
    store = analytics.get_analytics_store()

    def fail(_record):
        raise sqlite3.OperationalError("read only")

    monkeypatch.setattr(store, "complete", fail)
    app = FastAPI()
    app.add_middleware(RequestAnalyticsMiddleware)

    @app.post("/v1/chat/completions")
    async def endpoint():
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/v1/chat/completions", json={"model": "test"})
    assert response.json() == {"ok": True}
    assert store.active == {}


def test_usage_hook_handles_unknown_and_malformed_values():
    record = RequestRecord()
    token = current_request.set(record)
    try:
        record_request_usage(
            "models/test",
            "success",
            {"prompt_tokens": "invalid", "completion_tokens": 3, "cached_tokens": -2},
            "STOP",
        )
    finally:
        current_request.reset(token)
    assert record.model == "test"
    assert record.prompt_tokens == 0
    assert record.total_tokens == 3
    assert record.cached_tokens == 0
    assert record.usage_known


@pytest.mark.asyncio
async def test_gemini_path_metadata_cannot_be_overridden_by_ignored_body_fields(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(tmp_path))
    app = FastAPI()
    app.add_middleware(RequestAnalyticsMiddleware)

    @app.post("/v1beta/models/path-model:generateContent")
    async def endpoint(request: Request):
        await request.json()
        return {"error": "example failure before usage hook"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/v1beta/models/path-model:generateContent",
            json={"model": "ignored-model", "stream": True},
        )
    row = _query(analytics.get_analytics_store())["requests"]["items"][0]
    assert row["model"] == "path-model"
    assert row["stream"] is False


@pytest.mark.asyncio
async def test_analytics_route_auth_validation_and_original_stats(
    monkeypatch, tmp_path
):
    from aistudio_api.api.app import app
    from aistudio_api.config import settings

    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "web_password", "test-admin-password")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/stats/analytics")).status_code == 401
        headers = {"authorization": "Bearer test-admin-password"}
        response = await client.get(
            "/stats/analytics?range=today&granularity=hour", headers=headers
        )
        assert response.status_code == 200
        assert len(response.json()["timeseries"]) == 24
        assert (
            await client.get("/stats/analytics?range=forever", headers=headers)
        ).status_code == 400
        assert (
            await client.get("/stats/analytics?page_size=500", headers=headers)
        ).status_code == 400
        legacy = (await client.get("/stats", headers=headers)).json()
        assert set(legacy) == {"models", "totals"}
