"""Exercise background generation after HTTP subscription ends."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest

from aistudio_api.api import reply_cache_tasks as task_module
from aistudio_api.api.reply_cache_tasks import BufferedReplyStream, ReplyCacheTasks


@pytest.mark.asyncio
async def test_disconnected_subscriber_does_not_stop_generation() -> None:
    tasks = ReplyCacheTasks()
    bridge = BufferedReplyStream()
    completed = asyncio.Event()
    backpressured = asyncio.Event()

    async def generate() -> AsyncGenerator[str, None]:
        for index in range(30):
            if index == 5:
                backpressured.set()
            yield str(index)
        completed.set()

    producer = tasks.start(lambda: bridge.produce(generate()))
    assert producer is not None
    events = bridge.events(producer)
    try:
        assert await anext(events) == "0"
        await asyncio.wait_for(backpressured.wait(), timeout=1)
        assert not completed.is_set()
        await events.aclose()
        await asyncio.wait_for(asyncio.shield(producer), timeout=1)
        assert completed.is_set()
    finally:
        await events.aclose()
        await tasks.shutdown()


@pytest.mark.asyncio
async def test_completion_failure_reaches_subscriber_after_queued_output() -> None:
    tasks = ReplyCacheTasks()
    bridge = BufferedReplyStream()

    async def generate() -> AsyncGenerator[str, None]:
        yield "partial reply"
        raise ValueError("source failed")

    producer = tasks.start(lambda: bridge.produce(generate()))
    assert producer is not None
    events = bridge.events(producer)
    try:
        bridge_chunk = await asyncio.wait_for(anext(events), timeout=1)
        assert bridge_chunk == "partial reply"
        with pytest.raises(ValueError, match="source failed"):
            await asyncio.wait_for(anext(events), timeout=1)
    finally:
        await events.aclose()
        await tasks.shutdown()


@pytest.mark.asyncio
async def test_capacity_and_shutdown_release_jobs() -> None:
    tasks = ReplyCacheTasks()
    started = asyncio.Event()
    count = 0
    closed = 0

    async def generate() -> None:
        nonlocal count, closed
        count += 1
        if count == 4:
            started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed += 1

    jobs = [tasks.start(generate) for _ in range(4)]
    assert all(job is not None for job in jobs)
    assert tasks.start(generate) is None
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
    finally:
        await tasks.shutdown()
    assert closed == 4
    assert all(job is not None and job.cancelled() for job in jobs)


@pytest.mark.asyncio
async def test_generation_deadline_closes_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(task_module, "_MAX_GENERATION_SECONDS", 0.01)
    tasks = ReplyCacheTasks()
    bridge = BufferedReplyStream()
    closed = asyncio.Event()

    async def generate() -> AsyncGenerator[str, None]:
        try:
            await asyncio.Event().wait()
            yield "unreachable"
        finally:
            closed.set()

    producer = tasks.start(lambda: bridge.produce(generate()))
    assert producer is not None
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(producer), timeout=1)
        assert closed.is_set()
    finally:
        await tasks.shutdown()
