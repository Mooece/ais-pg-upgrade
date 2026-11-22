"""Request lifecycle metadata without payloads, credentials or exception messages."""

from __future__ import annotations

import json
import sys
from contextvars import ContextVar
from importlib.metadata import version

from .logger import get_logger

logger = get_logger("request_diagnostics")
request_diagnostic_id: ContextVar[str | None] = ContextVar(
    "request_diagnostic_id", default=None
)
_RUNTIME_VERSIONS = {
    "python_version": sys.version.split()[0],
    "starlette_version": version("starlette"),
    "uvicorn_version": version("uvicorn"),
    "anyio_version": version("anyio"),
}
_METADATA_FIELDS = frozenset(
    {
        "endpoint",
        "asgi_spec_version",
        "elapsed_ms",
        "status_code",
        "response_mode",
        "response_complete",
        "message_type",
        "exception_type",
        "exception_errno",
        "errno",
        "sent_bytes",
        "last_send_elapsed_ms",
        "termination",
        "status",
        "http_disconnect_observed",
        "delivery_confirmation",
        "result_status",
        "read_timeout_s",
        "received_bytes",
        "received_chunks",
        "last_byte_age_ms",
        "reason",
    }
)


def log_request_event(event: str, **fields: object) -> None:
    """Log explicit lifecycle fields only for observed generation requests.

    Callers must supply counters, phases and exception class names, never
    exception messages, URLs, headers or model input/output.
    """
    request_id = request_diagnostic_id.get()
    if request_id is None:
        return
    payload: dict[str, object] = {"request_id": request_id, "event": event}
    if event == "request_start":
        payload.update(_RUNTIME_VERSIONS)
    for key, value in fields.items():
        if key not in _METADATA_FIELDS:
            continue
        if value is None or isinstance(value, (bool, int, float)):
            payload[key] = value
        elif isinstance(value, str):
            payload[key] = value[:160]
    logger.info("request_lifecycle %s", json.dumps(payload, ensure_ascii=True))
