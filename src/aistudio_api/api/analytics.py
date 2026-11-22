"""Bounded, metadata-only request analytics backed by SQLite.

The legacy model counters remain independent: a row here represents one client
request, even when the gateway retries several upstream accounts.
"""

from __future__ import annotations

import asyncio
import math
import os
import sqlite3
import threading
import time
from contextlib import closing, suppress
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from aistudio_api.config import settings
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("analytics")
RETENTION_DAYS = 90
MAX_RECORDS = 100_000
RangeName = Literal["today", "7d", "30d", "month", "last_month"]
Granularity = Literal["hour", "day"]
RequestStatus = Literal["active", "success", "error", "rate_limited", "cancelled"]
TOKEN_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "total_tokens",
)


def _integer(value: object) -> int:
    try:
        return max(0, int(str(value or 0)))
    except (ValueError, TypeError, OverflowError):
        return 0


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


@dataclass
class RequestRecord:
    id: str = field(default_factory=lambda: uuid4().hex)
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    model: str = "unknown"
    endpoint: str = "/v1/chat/completions"
    stream: bool = False
    status: RequestStatus = "active"
    status_code: int | None = None
    duration_ms: float = 0.0
    ttft_ms: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    finish_reason: str | None = None
    usage_known: bool = False
    _started_monotonic: float = field(default_factory=time.perf_counter, repr=False)
    _last_result: str | None = field(default=None, repr=False)

    @property
    def started_ts(self) -> float:
        return datetime.fromisoformat(self.started_at).timestamp()

    def public(self) -> dict[str, object]:
        payload = {
            key: value for key, value in asdict(self).items() if not key.startswith("_")
        }
        if self.status == "active":
            payload["duration_ms"] = round(
                (time.perf_counter() - self._started_monotonic) * 1000, 2
            )
        return payload


current_request: ContextVar[RequestRecord | None] = ContextVar(
    "analytics_request", default=None
)


def record_request_usage(
    model: str,
    result: str,
    usage: dict[str, object] | None,
    finish_reason: str | None,
) -> None:
    """Called by legacy RuntimeState.record without changing legacy counters."""
    record = current_request.get()
    if record is None:
        return
    record.model = model.removeprefix("models/")[:200]
    record._last_result = result
    if finish_reason:
        record.finish_reason = str(finish_reason)[:80]
    if usage is not None:
        details = usage.get("completion_tokens_details")
        record.prompt_tokens = _integer(usage.get("prompt_tokens"))
        record.completion_tokens = _integer(usage.get("completion_tokens"))
        record.reasoning_tokens = (
            _integer(details.get("reasoning_tokens"))
            if isinstance(details, dict)
            else 0
        )
        record.cached_tokens = _integer(usage.get("cached_tokens"))
        record.total_tokens = (
            _integer(usage.get("total_tokens"))
            or record.prompt_tokens + record.completion_tokens
        )
        record.usage_known = True


def period_bounds(
    range_name: RangeName, offset: int, now: datetime
) -> tuple[datetime, datetime]:
    local = now.astimezone(timezone(timedelta(minutes=offset)))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if range_name == "last_month":
        end = midnight.replace(day=1)
        start = (end - timedelta(days=1)).replace(day=1)
    else:
        end = midnight + timedelta(days=1)
        start = (
            midnight.replace(day=1)
            if range_name == "month"
            else midnight - timedelta(days={"today": 0, "7d": 6, "30d": 29}[range_name])
        )
    return start.astimezone(UTC), end.astimezone(UTC)


def _empty_summary() -> dict[str, int | float]:
    return dict.fromkeys(
        (
            "requests",
            "success",
            "errors",
            "rate_limited",
            "cancelled",
            *TOKEN_FIELDS,
            "duration_sum",
            "duration_count",
            "ttft_sum",
            "ttft_count",
        ),
        0,
    )


def _add_summary(
    target: dict[str, int | float], source: dict[str, int | float]
) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


def _public_summary(summary: dict[str, int | float]) -> dict[str, object]:
    output: dict[str, object] = {
        key: value
        for key, value in summary.items()
        if key not in ("duration_sum", "duration_count", "ttft_sum", "ttft_count")
    }
    output["avg_duration_ms"] = (
        round(summary["duration_sum"] / summary["duration_count"], 2)
        if summary["duration_count"]
        else None
    )
    return output


class AnalyticsStore:
    """Connections live only inside worker calls; no unbounded in-memory history."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.active: dict[str, RequestRecord] = {}
        self.lock = threading.RLock()
        self._db_lock = threading.RLock()
        self.started_monotonic = time.perf_counter()
        self._last_prune = 0.0

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA cache_size=-1024")
            connection.execute("""CREATE TABLE IF NOT EXISTS requests (
            id TEXT PRIMARY KEY, started_at TEXT NOT NULL, started_ts REAL NOT NULL,
            model TEXT NOT NULL, endpoint TEXT NOT NULL, stream INTEGER NOT NULL,
            status TEXT NOT NULL, status_code INTEGER, duration_ms REAL NOT NULL,
            ttft_ms REAL, prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL,
            reasoning_tokens INTEGER NOT NULL, cached_tokens INTEGER NOT NULL,
            total_tokens INTEGER NOT NULL, finish_reason TEXT, usage_known INTEGER NOT NULL
        )""")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS requests_started ON requests(started_ts DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS requests_model_started ON requests(model, started_ts DESC)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('history_started_at', ?)",
                (_iso(time.time()),),
            )
            now = time.time()
            if now - self._last_prune >= 60:
                connection.execute(
                    "DELETE FROM requests WHERE started_ts < ?",
                    (now - RETENTION_DAYS * 86400,),
                )
                self._last_prune = now
            connection.commit()
            return connection
        except Exception:
            connection.close()
            raise

    def begin(self, record: RequestRecord) -> None:
        with self.lock:
            self.active[record.id] = record

    def complete(self, record: RequestRecord) -> None:
        """Atomic replacement of live metadata with a durable completed record."""
        payload = record.public()
        payload["started_ts"] = record.started_ts
        columns = list(payload)
        with self._db_lock:
            try:
                with closing(self._connect()) as connection, connection:
                    connection.execute(
                        f"INSERT OR REPLACE INTO requests ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                        tuple(payload.values()),
                    )
                    if (
                        connection.execute("SELECT COUNT(*) FROM requests").fetchone()[
                            0
                        ]
                        > MAX_RECORDS
                    ):
                        connection.execute(
                            """DELETE FROM requests WHERE id IN (
                            SELECT id FROM requests ORDER BY started_ts DESC, id DESC LIMIT -1 OFFSET ?
                        )""",
                            (MAX_RECORDS,),
                        )
            finally:
                with self.lock:
                    self.active.pop(record.id, None)

    def query(
        self,
        range_name: RangeName = "7d",
        granularity: Granularity = "day",
        model: str | None = None,
        status: RequestStatus | None = None,
        page: int = 1,
        page_size: int = 20,
        timezone_offset: int = 480,
        now: datetime | None = None,
    ) -> dict[str, object]:
        current_time = now or datetime.now(UTC)
        start, end = period_bounds(range_name, timezone_offset, current_time)
        start_ts, end_ts = start.timestamp(), end.timestamp()
        step = 3600 if granularity == "hour" else 86400
        count = math.ceil((end_ts - start_ts) / step)
        totals = _empty_summary()
        buckets = [_empty_summary() for _ in range(count)]
        model_summaries: dict[str, dict[str, int | float]] = {}
        endpoint_summaries: dict[str, dict[str, int | float]] = {}
        conditions = ["started_ts >= ?", "started_ts < ?"]
        params: list[object] = [start_ts, end_ts]
        if model:
            conditions.append("model = ?")
            params.append(model)
        if status:
            conditions.append("status = ?")
            params.append(status)
        where = " AND ".join(conditions)
        aggregates = ", ".join(
            [
                "COUNT(*) AS requests",
                "SUM(status='success') AS success",
                "SUM(status='error') AS errors",
                "SUM(status='rate_limited') AS rate_limited",
                "SUM(status='cancelled') AS cancelled",
                *(f"SUM({key}) AS {key}" for key in TOKEN_FIELDS),
                "SUM(duration_ms) AS duration_sum",
                "COUNT(*) AS duration_count",
                "SUM(CASE WHEN status='success' AND stream=1 THEN ttft_ms ELSE 0 END) AS ttft_sum",
                "SUM(status='success' AND stream=1 AND ttft_ms IS NOT NULL) AS ttft_count",
            ]
        )
        with self._db_lock, closing(self._connect()) as connection:
            with self.lock:
                active_snapshot = [record.public() for record in self.active.values()]
            rows = connection.execute(
                f"SELECT model, endpoint, CAST((started_ts-?)/? AS INTEGER) AS bucket_index, {aggregates} "
                f"FROM requests WHERE {where} GROUP BY model, endpoint, bucket_index",
                [start_ts, step, *params],
            ).fetchall()
            for row in rows:
                summary = {key: row[key] or 0 for key in totals}
                _add_summary(totals, summary)
                _add_summary(buckets[row["bucket_index"]], summary)
                name, endpoint = row["model"], row["endpoint"]
                model_summaries.setdefault(name, _empty_summary())
                endpoint_summaries.setdefault(endpoint, _empty_summary())
                _add_summary(model_summaries[name], summary)
                _add_summary(endpoint_summaries[endpoint], summary)
            completed_count = int(totals["duration_count"])
            p95_row = (
                connection.execute(
                    f"SELECT duration_ms FROM requests WHERE {where} ORDER BY duration_ms LIMIT 1 OFFSET ?",
                    [*params, max(0, math.ceil(completed_count * 0.95) - 1)],
                ).fetchone()
                if completed_count
                else None
            )
            # Live rows can be older than recently completed ones. Merge in SQL
            # before paging so the same global ordering applies to every page.
            active_items = [
                record
                for record in active_snapshot
                if start_ts
                <= datetime.fromisoformat(str(record["started_at"])).timestamp()
                < end_ts
                and (not model or record["model"] == model)
                and (not status or status == "active")
            ]
            for record in active_items:
                name, endpoint = str(record["model"]), str(record["endpoint"])
                index = min(
                    count - 1,
                    int(
                        (
                            datetime.fromisoformat(
                                str(record["started_at"])
                            ).timestamp()
                            - start_ts
                        )
                        // step
                    ),
                )
                summary = _empty_summary()
                summary["requests"] = 1
                model_summaries.setdefault(name, _empty_summary())
                endpoint_summaries.setdefault(endpoint, _empty_summary())
                for target in (
                    totals,
                    buckets[index],
                    model_summaries[name],
                    endpoint_summaries[endpoint],
                ):
                    _add_summary(target, summary)
            columns = list(RequestRecord().public())
            columns.append("started_ts")
            select_columns = ",".join(columns)
            live_values: list[list[object]] = []
            for item in active_items:
                item["started_ts"] = datetime.fromisoformat(
                    str(item["started_at"])
                ).timestamp()
                live_values.append([item[key] for key in columns])
            query = f"SELECT {select_columns} FROM requests WHERE {where}"
            if live_values:
                connection.execute(
                    f"CREATE TEMP TABLE live AS SELECT {select_columns} FROM requests WHERE 0"
                )
                connection.executemany(
                    f"INSERT INTO live VALUES ({','.join('?' for _ in columns)})",
                    live_values,
                )
                query += f" UNION ALL SELECT {select_columns} FROM live"
            recent = [
                dict(row)
                for row in connection.execute(
                    query + " ORDER BY started_ts DESC, id DESC LIMIT ? OFFSET ?",
                    [*params, page_size, (page - 1) * page_size],
                )
            ]
            for record in recent:
                record.pop("started_ts", None)
                record["stream"] = bool(record["stream"])
                record["usage_known"] = bool(record["usage_known"])
            model_names = [
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT model FROM requests ORDER BY model"
                )
            ]
            model_names = sorted(
                set(model_names) | {str(record["model"]) for record in active_snapshot}
            )
            history = connection.execute(
                "SELECT value FROM metadata WHERE key='history_started_at'"
            ).fetchone()
            active_count = len(active_snapshot)

        tz = timezone(timedelta(minutes=timezone_offset))

        def series(summaries: list[dict[str, int | float]]) -> list[dict[str, object]]:
            output = []
            for index, summary in enumerate(summaries):
                timestamp = start_ts + index * step
                local = datetime.fromtimestamp(timestamp, tz)
                output.append(
                    {
                        "bucket": _iso(timestamp),
                        "label": local.strftime(
                            "%m-%d %H:00" if granularity == "hour" else "%m-%d"
                        ),
                        **_public_summary(summary),
                    }
                )
            return output

        completed = totals["duration_count"]
        public_totals = _public_summary(totals)
        public_totals.update(
            {
                "avg_ttft_ms": round(totals["ttft_sum"] / totals["ttft_count"], 2)
                if totals["ttft_count"]
                else None,
                "p95_duration_ms": round(p95_row[0], 2) if p95_row else None,
                "success_rate": round(totals["success"] / completed * 100, 2)
                if completed
                else 0,
                "active_requests": active_count,
                "uptime_seconds": round(time.perf_counter() - self.started_monotonic),
            }
        )
        return {
            "demo": os.getenv("AISTUDIO_ANALYTICS_DEMO") == "1",
            "range": range_name,
            "granularity": granularity,
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "retention_days": RETENTION_DAYS,
            "history_started_at": history[0] if history else None,
            "totals": public_totals,
            "timeseries": series(buckets),
            "models": [
                {
                    "name": name,
                    **_public_summary(summary),
                }
                for name, summary in sorted(
                    model_summaries.items(),
                    key=lambda item: item[1]["requests"],
                    reverse=True,
                )
            ],
            "endpoints": [
                {"name": name, **_public_summary(summary)}
                for name, summary in sorted(
                    endpoint_summaries.items(),
                    key=lambda item: item[1]["requests"],
                    reverse=True,
                )
            ],
            "requests": {
                "items": recent,
                "total": int(totals["requests"]),
                "page": page,
                "page_size": page_size,
            },
            "filters": {"models": model_names},
        }


_store: AnalyticsStore | None = None
_pending: set[asyncio.Task[None]] = set()


def get_analytics_store() -> AnalyticsStore:
    global _store
    # Path selection is cheap on the event loop; directory/database creation is
    # performed by _connect only inside the persistence/query worker thread.
    path = (
        Path(os.getenv("AISTUDIO_DATA_DIR") or settings.data_dir).absolute()
        / "request_analytics.sqlite3"
    )
    if _store is None or _store.path != path:
        _store = AnalyticsStore(path)
    return _store


async def finish_request(store: AnalyticsStore, record: RequestRecord) -> None:
    async def persist() -> None:
        try:
            await asyncio.to_thread(store.complete, record)
        except Exception:
            logger.warning("请求统计写入失败，模型响应不受影响", exc_info=True)
        finally:
            with store.lock:
                store.active.pop(record.id, None)

    task = asyncio.create_task(persist())
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    # Keep the final metadata write alive after a client disconnect.
    with suppress(asyncio.CancelledError):
        await asyncio.shield(task)


async def flush_analytics() -> None:
    if _pending:
        await asyncio.gather(*tuple(_pending), return_exceptions=True)
