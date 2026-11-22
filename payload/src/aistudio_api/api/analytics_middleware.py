"""Observe the complete ASGI response without retaining prompts or responses."""

from __future__ import annotations

import asyncio
import codecs
import json
import time

from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aistudio_api.infrastructure.utils.logger import get_logger
from aistudio_api.infrastructure.utils.request_diagnostics import (
    log_request_event,
    request_diagnostic_id,
)

from .analytics import (
    RequestRecord,
    current_request,
    finish_request,
    get_analytics_store,
)

logger = get_logger("analytics.middleware")


def _mark_error(
    record: RequestRecord, code: object = None, error_type: object = None
) -> None:
    numeric_code = (
        int(code) if isinstance(code, (int, str)) and str(code).isdigit() else None
    )
    record.status_code = (
        numeric_code
        if numeric_code is not None and 400 <= numeric_code <= 599
        else 429
        if error_type == "rate_limit_error"
        else record.status_code
        if record.status_code is not None and record.status_code >= 400
        else 502
    )
    record.status = "rate_limited" if record.status_code == 429 else "error"


class _JsonOutcomeObserver:
    """Constant-space JSON outcome scan, discarding response text and error messages.

    Large image/text/error strings are skipped, so an error code following a huge
    message is still visible. An incomplete or unrecognizable response is never
    promoted to success just because its HTTP headers were already sent.
    """

    def __init__(self, record: RequestRecord) -> None:
        self.record = record
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.depth = 0
        self.in_string = False
        self.escape = False
        self.expect_key = False
        self.key = ""
        self.capture = ""
        self.capture_kind = ""
        self.primitive = ""
        self.root_seen = False
        self.root_closed = False
        self.invalid = False
        self.error_seen = False
        self.error_depth: int | None = None
        self.error_code: object = None
        self.error_type: object = None
        self.sentinel = ""

    def feed(self, data: bytes) -> None:
        for char in self.decoder.decode(data):
            if self.in_string:
                if self.escape:
                    self.escape = False
                    if self.capture_kind and len(self.capture) < 256:
                        self.capture += "\\" + char
                elif char == "\\":
                    self.escape = True
                elif char == '"':
                    self.in_string = False
                    if self.capture_kind == "key":
                        try:
                            self.key = str(json.loads('"' + self.capture + '"'))
                        except ValueError:
                            self.invalid = True
                    elif self.capture_kind in ("type", "code"):
                        try:
                            self._error_value(
                                self.capture_kind, json.loads('"' + self.capture + '"')
                            )
                        except ValueError:
                            self.invalid = True
                    self.capture = self.capture_kind = ""
                elif self.capture_kind and len(self.capture) < 256:
                    self.capture += char
                continue
            if self.root_closed:
                if not char.isspace():
                    self.invalid = True
                continue
            if not self.root_seen and char.isspace():
                continue
            if not self.root_seen:
                if char == "[":
                    # SSE [DONE] is handled without storing arbitrary content.
                    self.sentinel = "["
                    self.root_seen = True
                    continue
                if char != "{":
                    self.invalid = True
                self.root_seen = True
            if self.sentinel:
                if len(self.sentinel) < 8:
                    self.sentinel += char
                else:
                    self.invalid = True
                continue
            if char == '"':
                self.in_string = True
                self.capture = ""
                self.capture_kind = (
                    "key"
                    if self.expect_key
                    else self.key
                    if self.depth == self.error_depth and self.key in ("code", "type")
                    else ""
                )
                if self.depth == 1 and self.key == "error" and not self.expect_key:
                    self.error_seen = True
            elif char in "{[":
                if self.depth == 1 and self.key == "error":
                    self.error_seen = True
                    self.error_depth = 2 if char == "{" else None
                self.depth += 1
                self.expect_key = char == "{"
                self.key = ""
            elif char in "}]":
                self._finish_primitive()
                if self.depth == self.error_depth:
                    self.error_depth = None
                self.depth -= 1
                if self.depth < 0:
                    self.invalid = True
                if self.depth == 0:
                    self.root_closed = True
                self.expect_key = False
                self.key = ""
            elif char == ":":
                self.expect_key = False
            elif char == ",":
                self._finish_primitive()
                self.expect_key = True
                self.key = ""
            elif not char.isspace() and len(self.primitive) < 64:
                self.primitive += char

    def _error_value(self, key: str, value: object) -> None:
        if key == "code":
            self.error_code = value
        elif key == "type":
            self.error_type = value

    def _finish_primitive(self) -> None:
        if (
            self.depth == 1
            and self.key == "error"
            and self.primitive
            and self.primitive != "null"
        ):
            self.error_seen = True
        if (
            self.primitive
            and self.depth == self.error_depth
            and self.key in ("code", "type")
        ):
            self._error_value(self.key, self.primitive)
        self.primitive = ""

    def finish(self) -> bool:
        if self.sentinel.strip() == "[DONE]":
            return True
        complete = (
            self.root_seen
            and self.root_closed
            and not self.invalid
            and not self.in_string
            and self.depth == 0
        )
        if self.error_seen or not complete:
            _mark_error(self.record, self.error_code, self.error_type)
        return complete


class _RequestMetadata:
    """Incrementally read only top-level model/stream JSON fields.

    No request body is buffered, including when model follows a very long prompt.
    String capture is capped and all other string contents are discarded.
    """

    def __init__(self, record: RequestRecord) -> None:
        self.record = record
        self.depth = 0
        self.in_string = False
        self.escape = False
        self.expect_key = True
        self.key = ""
        self.capture = ""
        self.capture_kind = ""
        self.primitive = ""

    def feed(self, data: bytes) -> None:
        for char in data.decode("utf-8", errors="replace"):
            if self.in_string:
                if self.escape:
                    self.escape = False
                    if self.capture_kind and len(self.capture) < 256:
                        self.capture += "\\" + char
                elif char == "\\":
                    self.escape = True
                elif char == '"':
                    self.in_string = False
                    if self.capture_kind == "key":
                        self.key = self.capture
                    elif self.capture_kind == "model":
                        try:
                            value = json.loads('"' + self.capture + '"')
                            self.record.model = str(value).removeprefix("models/")[:200]
                        except ValueError:
                            pass
                    self.capture = self.capture_kind = ""
                elif self.capture_kind and len(self.capture) < 256:
                    self.capture += char
                continue
            if char == '"':
                self.in_string = True
                self.capture = ""
                self.capture_kind = (
                    "key"
                    if self.depth == 1 and self.expect_key
                    else "model"
                    if self.depth == 1 and self.key == "model"
                    else ""
                )
            elif char in "{[":
                self.depth += 1
            elif char in "}]":
                self._finish_primitive()
                self.depth -= 1
            elif self.depth == 1:
                if char == ":":
                    self.expect_key = False
                elif char == ",":
                    self._finish_primitive()
                    self.expect_key = True
                    self.key = ""
                elif (
                    self.key == "stream"
                    and not char.isspace()
                    and len(self.primitive) < 8
                ):
                    self.primitive += char

    def _finish_primitive(self) -> None:
        if self.depth == 1 and self.key == "stream":
            self.record.stream = self.primitive == "true"
        self.primitive = ""


def _has_content(payload: dict[str, object]) -> bool:
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict) and any(
                delta.get(key)
                for key in (
                    "content",
                    "reasoning_content",
                    "reasoning",
                    "tool_calls",
                    "function_call",
                    "images",
                    "audio",
                )
            ):
                return True
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            content = candidate.get("content") if isinstance(candidate, dict) else None
            parts = content.get("parts") if isinstance(content, dict) else None
            if isinstance(parts, list) and any(
                isinstance(part, dict)
                and any(
                    part.get(key)
                    for key in ("text", "functionCall", "inlineData", "fileData")
                )
                for part in parts
            ):
                return True
    return False


class _StreamObserver:
    """Bounded SSE frame parser; TTFT excludes role/keepalive/usage frames."""

    MAX_FRAME_BYTES = 64 * 1024

    def __init__(self, record: RequestRecord) -> None:
        self.record = record
        self.pending = b""
        self.overflow = False
        self.error_seen = False
        self.line_nonempty = False
        self.outcome: _JsonOutcomeObserver | None = None
        self.line_prefix = b""
        self.data_line = False
        self.events = 0

    def feed(self, body: bytes) -> None:
        position = 0
        while position < len(body):
            newline = body.find(b"\n", position)
            end = newline + 1 if newline >= 0 else len(body)
            self._observe_outcome(body[position:end])
            if not self.overflow:
                remaining = self.MAX_FRAME_BYTES - len(self.pending)
                self.pending += body[position : min(end, position + remaining)]
                self.overflow = end - position > remaining
            content_end = newline if newline >= 0 else end
            if content_end > position and not (
                content_end == position + 1 and body[position] == 13
            ):
                self.line_nonempty = True
            if newline >= 0 and not self.line_nonempty:
                self._event()
                self._finish_outcome()
                self.pending = b""
                self.overflow = False
            if newline >= 0:
                self.line_nonempty = False
                self.line_prefix = b""
                self.data_line = False
            position = end

    def finish(self) -> None:
        if self.pending:
            self._event()
            self._finish_outcome()

    def _observe_outcome(self, fragment: bytes) -> None:
        if self.data_line:
            if self.outcome is not None:
                self.outcome.feed(fragment)
            return
        need = 5 - len(self.line_prefix)
        self.line_prefix += fragment[:need]
        if len(self.line_prefix) == 5 and self.line_prefix == b"data:":
            self.data_line = True
            if self.outcome is None:
                self.outcome = _JsonOutcomeObserver(self.record)
            self.outcome.feed(fragment[need:])

    def _finish_outcome(self) -> None:
        if self.outcome is not None:
            complete = self.outcome.finish()
            self.events += 1
            if self.outcome.error_seen or not complete:
                self.error_seen = True
            self.outcome = None

    def _event(self) -> None:
        data = b"\n".join(
            line[5:].strip()
            for line in self.pending.splitlines()
            if line.startswith(b"data:")
        )
        if not data or data == b"[DONE]":
            return
        try:
            payload = json.loads(data)
        except ValueError:
            # A large multimodal event can exceed the cap. The field prefix is
            # sufficient to detect first output; the actual image is not retained.
            if self.overflow and any(
                marker in data
                for marker in (
                    b'"inlineData"',
                    b'"fileData"',
                    b'"images"',
                    b'"audio"',
                    b'"content": "',
                    b'"content":"',
                    b'"text": "',
                    b'"text":"',
                    b'"tool_calls"',
                    b'"functionCall"',
                )
            ):
                self._mark_content()
            return
        if not isinstance(payload, dict):
            return
        error = payload.get("error")
        if isinstance(error, dict):
            self.error_seen = True
            _mark_error(self.record, error.get("code"), error.get("type"))
        elif _has_content(payload):
            self._mark_content()

    def _mark_content(self) -> None:
        if self.record.ttft_ms is None:
            self.record.ttft_ms = round(
                (time.perf_counter() - self.record._started_monotonic) * 1000, 2
            )


def _request_target(scope: Scope) -> tuple[str, str, bool] | None:
    if scope["type"] != "http" or scope.get("method") != "POST":
        return None
    path = str(scope.get("path", ""))
    if path.rstrip("/") == "/v1/chat/completions":
        return "unknown", "/v1/chat/completions", False
    if path.startswith("/v1beta/") and path.endswith(
        (":generateContent", ":streamGenerateContent")
    ):
        model, _, action = path.removeprefix("/v1beta/").rpartition(":")
        return (
            model.removeprefix("models/")[:200],
            f"/v1beta/models/*:{action}",
            action == "streamGenerateContent",
        )
    if path.rstrip("/") == "/v1beta/decisions":
        return "gemini-decision-1", "/v1beta/decisions", False
    return None


class RequestAnalyticsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        target = _request_target(scope)
        if target is None:
            await self.app(scope, receive, send)
            return
        record = RequestRecord(model=target[0], endpoint=target[1], stream=target[2])
        try:
            store = get_analytics_store()
            store.begin(record)
        except Exception:
            logger.warning("请求统计初始化失败，继续处理模型请求", exc_info=True)
            await self.app(scope, receive, send)
            return
        token = current_request.set(record)
        diagnostic_token = request_diagnostic_id.set(record.id)
        metadata = _RequestMetadata(record)
        observer = _StreamObserver(record)
        json_observer = _JsonOutcomeObserver(record)
        response_complete = False
        disconnected = False
        disconnect_observed = False
        send_failed = False
        is_event_stream = False
        is_json_response = False
        sent_bytes = 0
        last_send_elapsed_ms: float | None = None
        termination = "incomplete_return"

        def elapsed_ms() -> float:
            return round((time.perf_counter() - record._started_monotonic) * 1000, 2)

        log_request_event(
            "request_start",
            endpoint=record.endpoint,
            asgi_spec_version=scope.get("asgi", {}).get("spec_version", "2.0"),
        )

        async def observed_receive() -> Message:
            nonlocal disconnected, disconnect_observed
            message = await receive()
            if message["type"] == "http.request" and record.endpoint in (
                "/v1/chat/completions",
                "/v1beta/decisions",
            ):
                try:
                    metadata.feed(message.get("body", b""))
                    if record.endpoint == "/v1beta/decisions":
                        record.stream = False
                except Exception:
                    logger.debug("请求统计无法读取模型元数据")
            elif message["type"] == "http.disconnect":
                disconnect_observed = True
                disconnected = not response_complete
                log_request_event(
                    "http_disconnect",
                    elapsed_ms=elapsed_ms(),
                    response_complete=response_complete,
                )
            return message

        async def observed_send(message: Message) -> None:
            nonlocal \
                response_complete, \
                is_event_stream, \
                is_json_response, \
                sent_bytes, \
                last_send_elapsed_ms, \
                send_failed
            if message["type"] == "http.response.start":
                record.status_code = int(message["status"])
                headers = dict(message.get("headers", []))
                content_type = headers.get(b"content-type", b"")
                is_event_stream = b"text/event-stream" in content_type
                is_json_response = b"application/json" in content_type
                if is_event_stream:
                    record.stream = True
            elif message["type"] == "http.response.body":
                if is_event_stream:
                    try:
                        observer.feed(message.get("body", b""))
                    except Exception:
                        logger.debug("请求统计无法解析流式元数据")
                elif is_json_response:
                    try:
                        json_observer.feed(message.get("body", b""))
                    except Exception:
                        json_observer.invalid = True
                        logger.debug("请求统计无法解析 JSON 元数据")
            try:
                await send(message)
            except BaseException as exc:
                send_failed = True
                log_request_event(
                    "send_failed",
                    elapsed_ms=elapsed_ms(),
                    message_type=message["type"],
                    exception_type=type(exc).__name__,
                    errno=getattr(exc, "errno", None),
                    sent_bytes=sent_bytes,
                )
                raise
            last_send_elapsed_ms = elapsed_ms()
            if message["type"] == "http.response.start":
                log_request_event(
                    "response_start",
                    elapsed_ms=last_send_elapsed_ms,
                    status_code=record.status_code,
                    response_mode="sse"
                    if is_event_stream
                    else "json"
                    if is_json_response
                    else "other",
                )
            elif message["type"] == "http.response.body":
                sent_bytes += len(message.get("body", b""))
                if not message.get("more_body", False):
                    response_complete = True

        try:
            await self.app(scope, observed_receive, observed_send)
            if is_event_stream:
                observer.finish()
            elif is_json_response:
                json_observer.finish()
            outcome_error = (
                observer.error_seen
                if is_event_stream
                else json_observer.error_seen
                or record.status in ("error", "rate_limited")
                if is_json_response
                else False
            )
            if not outcome_error:
                if disconnected or not response_complete:
                    record.status = "cancelled"
                elif record.status_code == 429:
                    record.status = "rate_limited"
                elif record.status_code is None or record.status_code >= 400:
                    record.status = "error"
                elif record._last_result in ("errors", "rate_limited"):
                    record.status = (
                        "rate_limited"
                        if record._last_result == "rate_limited"
                        else "error"
                    )
                else:
                    record.status = "success"
            termination = (
                "http_disconnect"
                if disconnected
                else "normal_complete"
                if response_complete
                else "incomplete_return"
            )
        except (asyncio.CancelledError, ConnectionError, ClientDisconnect) as exc:
            record.status = "cancelled"
            termination = (
                "send_failed"
                if send_failed
                else "cancelled_error"
                if isinstance(exc, asyncio.CancelledError)
                else "client_disconnect"
                if isinstance(exc, ClientDisconnect)
                else "connection_error"
            )
            log_request_event(
                termination,
                elapsed_ms=elapsed_ms(),
                exception_type=type(exc).__name__,
                errno=getattr(exc, "errno", None),
            )
            raise
        except Exception as exc:
            record.status = "cancelled" if send_failed else "error"
            termination = "send_failed" if send_failed else "application_error"
            log_request_event(
                termination,
                elapsed_ms=elapsed_ms(),
                exception_type=type(exc).__name__,
                errno=getattr(exc, "errno", None),
            )
            if not send_failed and (
                record.status_code is None or record.status_code < 400
            ):
                record.status_code = 500
            raise
        finally:
            record.duration_ms = round(
                (time.perf_counter() - record._started_monotonic) * 1000, 2
            )
            log_request_event(
                "request_end",
                elapsed_ms=record.duration_ms,
                termination=termination,
                status=record.status,
                status_code=record.status_code,
                response_complete=response_complete,
                http_disconnect_observed=disconnect_observed,
                sent_bytes=sent_bytes,
                last_send_elapsed_ms=last_send_elapsed_ms,
                asgi_spec_version=scope.get("asgi", {}).get("spec_version", "2.0"),
                delivery_confirmation="asgi_send_completed_only",
            )
            try:
                await finish_request(store, record)
            finally:
                current_request.reset(token)
                request_diagnostic_id.reset(diagnostic_token)
