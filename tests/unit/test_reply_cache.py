import asyncio
import os

import httpx
import pytest
from fastapi import Depends, FastAPI

from aistudio_api.api import reply_cache
from aistudio_api.api.dependencies import require_web_auth
from aistudio_api.api.reply_cache import ReplyCache
from aistudio_api.config import settings


def _completion(text: str) -> dict[str, object]:
    return {
        "model": "gemini-test",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }


@pytest.mark.asyncio
async def test_records_persist_and_eviction_preserves_newest_completion(tmp_path):
    path = tmp_path / "replies.sqlite3"
    cache = ReplyCache(path)
    assert not await cache.enabled()
    await cache.save("owner", _completion("default disabled"))
    assert not path.exists()

    await cache.configure(2)
    await cache.save("owner", _completion("第一轮"))
    first = (await cache.status())["entries"][0]["id"]
    await cache.save("owner", _completion("第二轮"))
    await cache.arm(first)

    reopened = ReplyCache(path)
    assert (await reopened.status())["pending_id"] == first
    await reopened.save("owner", _completion("第三轮"))
    status = await cache.status()
    assert [entry["preview"] for entry in status["entries"]] == ["第三轮", "第二轮"]
    assert status["pending_id"] is None
    assert await cache.arm(first) is None

    assert len((await cache.configure(1))["entries"]) == 1
    await cache.save("owner", _completion("第四轮"))
    assert [entry["preview"] for entry in (await cache.status())["entries"]] == [
        "第四轮"
    ]
    assert (await cache.configure(0))["entries"] == []
    assert not await cache.enabled()


@pytest.mark.asyncio
async def test_replay_is_atomic_scoped_and_leaves_entry_available(tmp_path):
    cache = ReplyCache(tmp_path / "replies.sqlite3")
    completion = _completion("one full reply")
    await cache.configure(2)
    await cache.save("key-a", completion)
    await cache.save("key-b", _completion("another owner's reply"))
    old = (await cache.status())["entries"][1]["id"]
    await cache.arm(old)

    assert await cache.take("key-b") is None
    assert (await cache.status())["pending_id"] == old
    results = await asyncio.gather(*(cache.take("key-a") for _ in range(6)))
    assert [item for item in results if item is not None] == [completion]
    status = await cache.status()
    assert status["pending_id"] is None and status["entries"][1]["id"] == old

    await cache.arm(old)
    assert (await cache.cancel())["pending_id"] is None
    assert await cache.take("key-a") is None


@pytest.mark.asyncio
async def test_oversized_reply_cannot_overwrite_the_last_good_reply(tmp_path):
    cache = ReplyCache(tmp_path / "replies.sqlite3")
    await cache.configure(1)
    await cache.save("owner", _completion("last good reply"))
    await cache.save("owner", _completion("中" * reply_cache.MAX_REPLY_BYTES))
    assert (await cache.status())["entries"][0]["preview"] == "last good reply"
    if os.name != "nt":
        assert cache.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_tool_arguments_and_signature_survive_persistence(tmp_path):
    completion: dict[str, object] = {
        "id": "chatcmpl-original",
        "model": "gemini-test",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_signed",
                            "type": "function",
                            "function": {
                                "name": "reply",
                                "arguments": '{"text":"完整回复"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"completion_tokens": 120},
    }
    cache = ReplyCache(tmp_path / "replies.sqlite3")
    await cache.configure(1)
    await cache.save("owner", completion)
    entry = (await cache.status())["entries"][0]
    assert entry["preview"] == "（工具调用）"
    await cache.arm(entry["id"])
    assert await ReplyCache(cache.path).take("owner") == completion


@pytest.fixture
def management_app(monkeypatch, tmp_path):
    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "api_keys", frozenset({"client-secret"}))
    monkeypatch.setattr(settings, "web_password", "admin-secret")
    app = FastAPI()
    app.include_router(reply_cache.router, dependencies=[Depends(require_web_auth)])
    return app


@pytest.mark.asyncio
async def test_panel_auth_required_for_preview_and_replay(management_app, monkeypatch):
    endpoint = "/api/reply-cache"
    headers = {"Authorization": "Bearer admin-secret"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=management_app), base_url="http://test"
    ) as client:
        response = await client.put(endpoint, json={"limit": 2}, headers=headers)
        assert response.status_code == 200
        await reply_cache.get_reply_cache().save("owner", _completion("private reply"))
        assert (await client.get(endpoint)).status_code == 401
        response = await client.get(
            endpoint, headers={"Authorization": "Bearer client-secret"}
        )
        assert response.status_code == 401

        response = await client.get(endpoint, headers=headers)
        assert response.headers["Cache-Control"] == "no-store"
        entry = response.json()["entries"][0]
        assert entry["preview"] == "private reply"
        assert "owner" not in entry and "payload" not in entry
        response = await client.post(
            f"{endpoint}/replay", json={"id": entry["id"]}, headers=headers
        )
        assert response.json()["pending_id"] == entry["id"]
        response = await client.delete(f"{endpoint}/replay", headers=headers)
        assert response.json()["pending_id"] is None
        response = await client.put(endpoint, json={"limit": 21}, headers=headers)
        assert response.status_code == 422

        monkeypatch.setattr(settings, "api_keys", frozenset())
        response = await client.get(endpoint, headers=headers)
        assert not response.json()["available"]
        assert response.json()["entries"] == []
        response = await client.put(endpoint, json={"limit": 1}, headers=headers)
        assert response.status_code == 403

        monkeypatch.setattr(settings, "web_password", None)
        response = await client.get(endpoint)
        assert not response.json()["available"]
        assert response.json()["entries"] == []
        response = await client.post(f"{endpoint}/replay", json={"id": entry["id"]})
        assert response.status_code == 403
