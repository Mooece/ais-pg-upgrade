"""Patch gateway contracts against the c770 upstream parser and wire model."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncGenerator

import pytest

from aistudio_api.domain.errors import RequestError, TransportError
from aistudio_api.domain.models import ThoughtSignatureEvent
from aistudio_api.infrastructure.gateway.client import PreparedRequest
from aistudio_api.infrastructure.gateway.stream_parser import (
    IncrementalJSONStreamParser,
)
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
from aistudio_api.infrastructure.gateway.wire_parser import parse_response_chunk
from aistudio_api.infrastructure.gateway.wire_spec import PartIndex
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart


class StreamSession:
    def __init__(self, raw: bytes):
        self.raw = raw

    async def send_streaming_request(
        self, **kwargs: object
    ) -> AsyncGenerator[tuple[str, object], None]:
        yield ("status", 200)
        for index in range(0, len(self.raw), 3):
            yield ("chunk", self.raw[index : index + 3])


def wire_response(parts: list[list[object]], finish: object = 1) -> bytes:
    candidate = [[parts, "model"], finish]
    chunk = [[candidate]]
    return json.dumps([[chunk]], ensure_ascii=False).encode("utf-8")


async def gateway_events(
    raw: bytes, *, strict: bool = False, model: str = "models/gemini-2.5-flash"
) -> list[tuple[str, object | None]]:
    gateway = StreamingGateway(session=StreamSession(raw))  # type: ignore[arg-type]
    request = PreparedRequest(
        url="https://example.invalid",
        headers={},
        body="{}",
        require_finish_reason=strict,
    )
    return [event async for event in gateway.stream_chat(request=request, model=model)]


@pytest.mark.parametrize(
    ("signature", "expected"),
    [("aGVsbG8", "aGVsbG8="), ("-_8", "+/8="), ("opaque!token", "opaque!token")],
)
def test_short_signatures_follow_upstream_canonical_normalization(signature, expected):
    part = AistudioPart(text="hello", thought_signature=signature)
    assert part.to_wire()[PartIndex.THOUGHT_SIGNATURE] == expected


def test_full_reply_sentinel_retains_wire_base64_encoding():
    sentinel = "skip_thought_signature_validator"
    part = AistudioPart(function_call=("answer", {}), thought_signature=sentinel)
    assert (
        part.to_wire()[PartIndex.THOUGHT_SIGNATURE]
        == base64.b64encode(sentinel.encode()).decode()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
async def test_ordered_signatures_and_literal_thought_tags_survive_patch(strict):
    first: list[object] = [None] * (PartIndex.THOUGHT_SIGNATURE + 1)
    first[PartIndex.TEXT] = "<thought>literal text</thought>🙂"
    first[PartIndex.THOUGHT_SIGNATURE] = "dGV4dA=="
    thought: list[object] = [None] * (PartIndex.THOUGHT_SIGNATURE + 1)
    thought[PartIndex.TEXT] = "reasoning"
    thought[PartIndex.THOUGHT_ALT_FLAG] = 1
    thought[PartIndex.THOUGHT_SIGNATURE] = "dGhvdWdodA=="
    events = await gateway_events(wire_response([first, thought]), strict=strict)
    assert [kind for kind, _ in events] == [
        "thought_signature",
        "body",
        "thought_signature",
        "thinking",
        "finish_reason",
        "usage",
        "done",
    ]
    assert events[1][1] == "<thought>literal text</thought>🙂"
    signatures = [payload for kind, payload in events if kind == "thought_signature"]
    first_signature, thought_signature = signatures
    assert isinstance(first_signature, ThoughtSignatureEvent)
    assert isinstance(thought_signature, ThoughtSignatureEvent)
    assert first_signature.thought is False
    assert first_signature.end_of_chunk is False
    assert thought_signature.thought is True
    assert thought_signature.end_of_chunk is True


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", [None, 0, 22])
async def test_gemini4_never_synthesizes_stop_for_nonterminal_finish(finish):
    with pytest.raises(TransportError, match="terminal finish_reason"):
        await gateway_events(
            wire_response([[None, "partial"]], finish), model="models/gemini-4-argon"
        )


@pytest.mark.asyncio
async def test_full_reply_keeps_real_terminal_requirement_for_legacy_models():
    with pytest.raises(TransportError, match="real finish reason"):
        await gateway_events(wire_response([[None, "partial"]], 22), strict=True)


@pytest.mark.asyncio
async def test_complete_legacy_stream_retains_default_stop():
    events = await gateway_events(wire_response([[None, "legacy answer"]], None))
    assert ("finish_reason", 1) in events
    assert events[-1] == ("done", None)


@pytest.mark.asyncio
async def test_policy_block_is_reported_in_full_reply_mode():
    text = (
        "The prompt could not be submitted. "
        "This prompt may violate Google's Prohibited Use Policy."
    )
    with pytest.raises(RequestError, match="blocked by Google policy"):
        await gateway_events(wire_response([[None, text]]), strict=True)


@pytest.mark.asyncio
async def test_truncated_normal_stream_does_not_synthesize_stop_after_repair():
    raw = wire_response([[None, "partial"]], None)
    with pytest.raises(TransportError, match="terminal finish_reason"):
        await gateway_events(raw[:-1])


@pytest.mark.asyncio
async def test_full_reply_rejects_truncated_json_and_invalid_utf8():
    raw = wire_response([[None, "complete"]])
    with pytest.raises(TransportError, match="incomplete JSON"):
        await gateway_events(raw[:-1], strict=True)
    with pytest.raises(TransportError, match="invalid UTF-8"):
        await gateway_events(raw.replace(b"complete", b"broken\xff"), strict=True)


def test_truncated_file_data_chunk_remains_recoverable_with_new_candidate_parts():
    part: list[object] = [None] * (PartIndex.FILE_DATA + 1)
    part[PartIndex.FILE_DATA] = ["files/synthetic", "video/mp4"]
    raw = wire_response([part], None).decode()
    raw = raw[: raw.index('"model"') + len('"model"')]
    parser = IncrementalJSONStreamParser()
    assert list(parser.feed(raw)) == []
    chunks = list(parser.finish())
    assert len(chunks) == 1
    assert [item.kind for item in parse_response_chunk(chunks[0]).parts] == [
        "file_data"
    ]
