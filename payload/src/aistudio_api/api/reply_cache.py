"""Private, bounded history and one-shot replay for completed OpenAI replies."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.model_defaults import get_configured_api_keys

MAX_REPLIES = 20
MAX_REPLY_BYTES = 2 * 1024 * 1024


class ReplyCacheEntry(TypedDict):
    id: str
    model: str
    created_at: str
    preview: str


class ReplyCacheStatus(TypedDict):
    available: bool
    limit: int
    entries: list[ReplyCacheEntry]
    pending_id: str | None


def _empty_status(*, available: bool) -> ReplyCacheStatus:
    return {"available": available, "limit": 0, "entries": [], "pending_id": None}


def _preview(completion: dict[str, object]) -> str:
    choices = completion.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    message = choice.get("message") if isinstance(choice, dict) else None
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            sample = content[:240].strip()
            if sample:
                return sample[:160] + ("…" if len(sample) > 160 else "")
        if message.get("tool_calls"):
            return "（工具调用）"
        if message.get("reasoning_content"):
            return "（思考输出）"
    return "（无文本预览）"


class ReplyCache:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(FileExistsError):
            self.path.touch(mode=0o600, exist_ok=False)
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA secure_delete=ON")
            db.execute("PRAGMA cache_size=-512")
            db.execute("""CREATE TABLE IF NOT EXISTS replies (
                seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, owner TEXT NOT NULL,
                model TEXT NOT NULL, created_at TEXT NOT NULL, preview TEXT NOT NULL,
                payload BLOB NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS cache_settings (
                singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                max_entries INTEGER NOT NULL DEFAULT 0,
                pending_id TEXT REFERENCES replies(id) ON DELETE SET NULL
            )""")
            with db:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield db
        finally:
            db.close()

    @staticmethod
    def _limit(db: sqlite3.Connection) -> int:
        row = db.execute("SELECT max_entries FROM cache_settings").fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _prune(db: sqlite3.Connection, limit: int) -> None:
        db.execute(
            """DELETE FROM replies WHERE seq IN (
                SELECT seq FROM replies ORDER BY seq DESC LIMIT -1 OFFSET ?
            )""",
            (limit,),
        )

    @staticmethod
    def _status(db: sqlite3.Connection) -> ReplyCacheStatus:
        row = db.execute(
            "SELECT max_entries, pending_id FROM cache_settings"
        ).fetchone()
        if row is None:
            return _empty_status(available=True)
        entries: list[ReplyCacheEntry] = [
            {"id": id_, "model": model, "created_at": created_at, "preview": preview}
            for id_, model, created_at, preview in db.execute(
                "SELECT id, model, created_at, preview FROM replies ORDER BY seq DESC"
            )
        ]
        return {
            "available": True,
            "limit": row[0],
            "entries": entries,
            "pending_id": row[1],
        }

    async def status(self) -> ReplyCacheStatus:
        def query() -> ReplyCacheStatus:
            if not self.path.exists():
                return _empty_status(available=True)
            with self._transaction() as db:
                return self._status(db)

        return await asyncio.to_thread(query)

    async def enabled(self) -> bool:
        def query() -> bool:
            if not self.path.exists():
                return False
            with self._transaction() as db:
                return self._limit(db) > 0

        return await asyncio.to_thread(query)

    async def configure(self, limit: int) -> ReplyCacheStatus:
        if not 0 <= limit <= MAX_REPLIES:
            raise ValueError("Reply cache limit is out of range")

        def write() -> ReplyCacheStatus:
            with self._transaction(write=True) as db:
                db.execute(
                    """INSERT INTO cache_settings (singleton, max_entries) VALUES (1, ?)
                    ON CONFLICT(singleton) DO UPDATE SET max_entries=excluded.max_entries""",
                    (limit,),
                )
                self._prune(db, limit)
                return self._status(db)

        return await asyncio.to_thread(write)

    async def save(self, owner: str, completion: dict[str, object]) -> None:
        if not owner:
            return

        def write() -> None:
            if not self.path.exists():
                return
            payload = json.dumps(
                completion, ensure_ascii=False, separators=(",", ":")
            ).encode()
            if len(payload) > MAX_REPLY_BYTES:
                return
            with self._transaction(write=True) as db:
                limit = self._limit(db)
                if limit == 0:
                    return
                db.execute(
                    """INSERT INTO replies (id, owner, model, created_at, preview, payload)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        uuid4().hex,
                        owner,
                        str(completion.get("model", "unknown"))[:200],
                        datetime.now(UTC).isoformat(),
                        _preview(completion),
                        payload,
                    ),
                )
                self._prune(db, limit)

        await asyncio.to_thread(write)

    async def arm(self, entry_id: str) -> ReplyCacheStatus | None:
        def write() -> ReplyCacheStatus | None:
            with self._transaction(write=True) as db:
                cursor = db.execute(
                    """UPDATE cache_settings SET pending_id=? WHERE max_entries>0
                    AND EXISTS(SELECT 1 FROM replies WHERE id=?)""",
                    (entry_id, entry_id),
                )
                return self._status(db) if cursor.rowcount else None

        return await asyncio.to_thread(write)

    async def cancel(self) -> ReplyCacheStatus:
        def write() -> ReplyCacheStatus:
            if not self.path.exists():
                return _empty_status(available=True)
            with self._transaction(write=True) as db:
                db.execute("UPDATE cache_settings SET pending_id=NULL")
                return self._status(db)

        return await asyncio.to_thread(write)

    async def take(self, owner: str) -> dict[str, object] | None:
        if not owner:
            return None

        def read_and_clear() -> dict[str, object] | None:
            if not self.path.exists():
                return None
            with self._transaction(write=True) as db:
                row = db.execute(
                    """SELECT replies.payload FROM cache_settings
                    JOIN replies ON replies.id=cache_settings.pending_id
                    WHERE cache_settings.max_entries>0 AND replies.owner=?""",
                    (owner,),
                ).fetchone()
                if not row:
                    return None
                completion = cast(dict[str, object], json.loads(row[0]))
                db.execute("UPDATE cache_settings SET pending_id=NULL")
                return completion

        return await asyncio.to_thread(read_and_clear)


def get_reply_cache() -> ReplyCache:
    data_dir = Path(os.getenv("AISTUDIO_DATA_DIR") or settings.data_dir).absolute()
    return ReplyCache(data_dir / "reply_cache.sqlite3")


def reply_cache_available() -> bool:
    return bool(
        settings.auth_enabled
        and settings.web_password
        and settings.web_password.strip()
        and (settings.api_keys or get_configured_api_keys())
    )


class RetentionRequest(BaseModel):
    limit: int = Field(ge=0, le=MAX_REPLIES, strict=True)


class ReplayRequest(BaseModel):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")


router = APIRouter(prefix="/api/reply-cache")


def _require_available() -> None:
    if not reply_cache_available():
        raise HTTPException(403, "回复缓存需要设定面板密码和 API Key")


@router.get("")
async def get_cache_status(response: Response) -> ReplyCacheStatus:
    response.headers["Cache-Control"] = "no-store"
    if not reply_cache_available():
        return _empty_status(available=False)
    return await get_reply_cache().status()


@router.put("")
async def set_cache_retention(
    body: RetentionRequest, response: Response
) -> ReplyCacheStatus:
    _require_available()
    response.headers["Cache-Control"] = "no-store"
    return await get_reply_cache().configure(body.limit)


@router.post("/replay")
async def arm_cache_replay(body: ReplayRequest, response: Response) -> ReplyCacheStatus:
    _require_available()
    response.headers["Cache-Control"] = "no-store"
    status = await get_reply_cache().arm(body.id)
    if status is None:
        raise HTTPException(404, "该缓存不存在或已被覆盖，请刷新列表")
    return status


@router.delete("/replay")
async def cancel_cache_replay(response: Response) -> ReplyCacheStatus:
    _require_available()
    response.headers["Cache-Control"] = "no-store"
    return await get_reply_cache().cancel()
