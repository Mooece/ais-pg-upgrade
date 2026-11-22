"""Keep cacheable generations alive independently of the HTTP subscriber."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncGenerator, Callable, Coroutine
from typing import TypeVar

from anyio import CancelScope

from aistudio_api.infrastructure.utils.request_diagnostics import log_request_event

_T = TypeVar("_T")
_MAX_TASKS = 4
_MAX_GENERATION_SECONDS = 1200
_BUFFERED_CHUNKS = 4


class ReplyCacheTasks:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[object]] = set()
        self._closing = False

    def start(
        self, factory: Callable[[], Coroutine[object, object, _T]]
    ) -> asyncio.Task[_T] | None:
        """Start a tracked job, or leave generation to the HTTP route at capacity."""
        if self._closing or len(self._tasks) >= _MAX_TASKS:
            return None

        async def run() -> _T:
            async with asyncio.timeout(_MAX_GENERATION_SECONDS):
                return await factory()

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._finish)
        return task

    def _finish(self, task: asyncio.Task[object]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            log_request_event(
                "reply_cache_generation_failed", exception_type=type(exc).__name__
            )

    async def shutdown(self) -> None:
        """Stop tracked jobs before closing the upstream client."""
        self._closing = True
        try:
            pending = tuple(self._tasks)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            self._closing = False


class BufferedReplyStream:
    """Bridge one generation to one optional SSE subscriber with backpressure."""

    def __init__(self) -> None:
        self._chunks: deque[str] = deque()
        self._changed = asyncio.Event()
        self._space = asyncio.Event()
        self._detached = False

    async def produce(self, source: AsyncGenerator[str, None]) -> None:
        try:
            async for chunk in source:
                while not self._detached and len(self._chunks) >= _BUFFERED_CHUNKS:
                    self._space.clear()
                    await self._space.wait()
                if not self._detached:
                    self._chunks.append(chunk)
                    self._changed.set()
        finally:
            with CancelScope(shield=True):
                await source.aclose()

    def detach(self) -> None:
        """Release queued output and wake a producer blocked on a slow client."""
        self._detached = True
        self._chunks.clear()
        self._space.set()
        self._changed.set()

    async def events(self, producer: asyncio.Task[None]) -> AsyncGenerator[str, None]:
        def wake(_producer: asyncio.Task[None]) -> None:
            self._changed.set()

        producer.add_done_callback(wake)
        try:
            while not self._detached:
                if self._chunks:
                    chunk = self._chunks.popleft()
                    self._space.set()
                    yield chunk
                elif producer.done():
                    producer.result()
                    return
                else:
                    self._changed.clear()
                    await self._changed.wait()
        finally:
            producer.remove_done_callback(wake)
            self.detach()


reply_cache_tasks = ReplyCacheTasks()
