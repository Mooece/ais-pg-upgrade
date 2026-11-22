"""Exercise actual httpcore/hyper-h2 framing without Google or credentials."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import h2.config
import h2.connection
import h2.events
import httpcore
import pytest
from httpcore._async.http2 import AsyncHTTP2Connection
from httpcore._backends.base import AsyncNetworkStream

from aistudio_api.infrastructure.gateway import transport


class H2Peer(AsyncNetworkStream):
    """An in-memory server: every byte is parsed by real hyper-h2 on both ends."""

    def __init__(self, on_request: Callable[[int], None]) -> None:
        self.server = h2.connection.H2Connection(
            config=h2.config.H2Configuration(client_side=False)
        )
        self.server.initiate_connection()
        self.incoming: asyncio.Queue[bytes] = asyncio.Queue()
        self.incoming.put_nowait(self.server.data_to_send())
        self.on_request = on_request
        self.pings: list[bytes] = []
        self.closed = False

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        try:
            return await asyncio.wait_for(self.incoming.get(), timeout)
        except TimeoutError as exc:
            raise httpcore.ReadTimeout from exc

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        for event in self.server.receive_data(buffer):
            if isinstance(event, h2.events.PingReceived):
                self.pings.append(event.ping_data)
            if isinstance(event, h2.events.RequestReceived):
                self.on_request(event.stream_id)
        self.flush()

    def flush(self) -> None:
        data = self.server.data_to_send()
        if data:
            self.incoming.put_nowait(data)

    def respond(self, stream_id: int, body: bytes = b"complete") -> None:
        self.server.send_headers(stream_id, [(b":status", b"200")])
        self.server.send_data(stream_id, body, end_stream=True)
        self.flush()

    async def aclose(self) -> None:
        self.closed = True


def request(read_timeout: float = 0.08) -> httpcore.Request:
    return httpcore.Request(
        "GET",
        "https://alkalimakersuite-pa.clients6.google.com/rpc",
        headers=[(b"host", b"alkalimakersuite-pa.clients6.google.com")],
        extensions={"timeout": {"read": read_timeout, "write": 1.0}},
    )


def connection(peer: H2Peer) -> AsyncHTTP2Connection:
    return AsyncHTTP2Connection(request().url.origin, peer)


@pytest.fixture(autouse=True)
def install(monkeypatch: pytest.MonkeyPatch) -> None:
    transport._install_http2_ping_keepalive()
    # Accelerated wall clock: 25 seconds => 0.01 seconds, 900 => 0.36.
    monkeypatch.setattr(transport, "H2_PING_INTERVAL_SECONDS", 0.01)


async def test_ping_ack_does_not_extend_pre_header_business_timeout() -> None:
    peer = H2Peer(lambda _stream_id: None)
    conn = connection(peer)
    # The safety guard makes the old ACK-reset bug fail without hanging pytest.
    with pytest.raises(httpcore.ReadTimeout):
        await asyncio.wait_for(conn.handle_async_request(request()), timeout=0.4)
    assert len(peer.pings) >= 2


async def test_pre_header_silence_exceeding_300_seconds_still_completes() -> None:
    # 0.14 seconds represents 350 seconds at the same scale as the PING period.
    loop = asyncio.get_running_loop()

    def schedule(stream_id: int) -> None:
        loop.call_later(0.14, peer.respond, stream_id)

    peer = H2Peer(schedule)
    conn = connection(peer)
    before = transport.get_upstream_keepalive_status()
    response = await conn.handle_async_request(request(read_timeout=0.36))
    assert await response.aread() == b"complete"
    await response.aclose()
    after = transport.get_upstream_keepalive_status()
    assert len(peer.pings) >= 5
    assert len(set(peer.pings)) == len(peer.pings)
    assert after["pings_sent"] > before["pings_sent"]
    assert after["acks_received"] > before["acks_received"]
    assert not peer.closed


async def test_body_data_resets_business_wait_but_control_frames_do_not() -> None:
    loop = asyncio.get_running_loop()

    def send_data(stream_id: int, data: bytes, ended: bool) -> None:
        peer.server.send_data(stream_id, data, end_stream=ended)
        peer.flush()

    def start_body(stream_id: int) -> None:
        peer.server.send_headers(stream_id, [(b":status", b"200")])
        peer.flush()
        loop.call_later(0.04, send_data, stream_id, b"first", False)
        loop.call_later(0.08, send_data, stream_id, b"second", True)

    peer = H2Peer(start_body)
    response = await connection(peer).handle_async_request(request(read_timeout=0.065))
    assert await response.aread() == b"firstsecond"
    await response.aclose()


async def test_ping_ack_does_not_extend_body_silence_timeout() -> None:
    def start_body(stream_id: int) -> None:
        peer.server.send_headers(stream_id, [(b":status", b"200")])
        peer.flush()

    peer = H2Peer(start_body)
    conn = connection(peer)
    response = await conn.handle_async_request(request(read_timeout=0.06))
    with pytest.raises(httpcore.ReadTimeout):
        await asyncio.wait_for(response.aread(), timeout=0.3)
    await response.aclose()
    assert len(peer.pings) >= 2


async def test_timed_out_stream_does_not_close_concurrent_stream() -> None:
    loop = asyncio.get_running_loop()
    seen: list[int] = []

    def schedule(stream_id: int) -> None:
        seen.append(stream_id)
        if len(seen) == 2:
            loop.call_later(0.12, peer.respond, stream_id)

    peer = H2Peer(schedule)
    conn = connection(peer)
    first = asyncio.create_task(conn.handle_async_request(request(read_timeout=0.06)))
    # First handshake advertises multiplexing before the second request starts.
    await asyncio.sleep(0.005)
    second = asyncio.create_task(conn.handle_async_request(request(read_timeout=0.3)))
    with pytest.raises(httpcore.ReadTimeout):
        await first
    assert conn._read_exception is None
    response = await second
    assert await response.aread() == b"complete"
    await response.aclose()
    assert not peer.closed
    assert not conn._events


async def test_cancellation_leaves_no_background_ping_or_connection_poison() -> None:
    peer = H2Peer(lambda _stream_id: None)
    conn = connection(peer)
    task = asyncio.create_task(conn.handle_async_request(request(read_timeout=0.3)))
    await asyncio.sleep(0.025)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    ping_count = len(peer.pings)
    await asyncio.sleep(0.025)
    assert len(peer.pings) == ping_count
    assert conn._read_exception is None
    assert not conn._events
    assert not peer.closed


def test_business_read_timeout_default_is_still_900_seconds() -> None:
    assert transport.HTTPStreamTransport().timeout == 900.0


@pytest.mark.parametrize("value", ["5", "120", "240", "25.5"])
def test_ping_interval_configuration_bounds(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISTUDIO_UPSTREAM_H2_PING_INTERVAL_SECONDS", value)
    assert transport._configured_ping_interval() == float(value)


@pytest.mark.parametrize("value", ["0", "4.9", "240.1", "nan", "inf", "bad"])
def test_invalid_ping_interval_uses_safe_default(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISTUDIO_UPSTREAM_H2_PING_INTERVAL_SECONDS", value)
    assert transport._configured_ping_interval() == 25.0


def test_incompatible_httpcore_hook_keeps_existing_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def incompatible(self: object) -> None:
        raise AssertionError("must never be called")

    monkeypatch.setattr(transport, "_h2_ping_installed", False)
    monkeypatch.setattr(AsyncHTTP2Connection, "_aistudio_h2_ping_installed", False)
    monkeypatch.setattr(AsyncHTTP2Connection, "_read_incoming_data", incompatible)
    transport._install_http2_ping_keepalive()
    assert not transport.get_upstream_keepalive_status()["installed"]
    assert "unavailable" in str(transport.get_upstream_keepalive_status()["last_error"])
    assert AsyncHTTP2Connection._read_incoming_data is incompatible
