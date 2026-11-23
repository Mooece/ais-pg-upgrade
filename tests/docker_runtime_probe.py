"""Exercise a patched image's API with synthetic replies and no external network.

Run inside the image with this file mounted read-only; it needs only the image's
normal Python dependencies. Actual server startup and signer checks are separate
Docker smoke checks, because ASGITransport does not run the application lifespan.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

MODEL = "gemini-4-argon[防截断]"
ANSWER = "Complete Docker reply.\nFinal paragraph."
SIGNATURE = "ZG9ja2VyLXByb2JlLXNpZw=="


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Synthetic Docker probe tool",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }


def _reply(name: str, arguments: object) -> dict:
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"text": "Synthetic private reasoning", "thought": True},
                        {
                            "functionCall": {
                                "name": name,
                                "id": "docker-probe-call",
                                "args": arguments,
                            },
                            "thoughtSignature": SIGNATURE,
                        },
                    ],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 5,
            "candidatesTokenCount": 7,
            "totalTokenCount": 12,
        },
    }


def _completion(response, stream: bool) -> dict:
    assert response.status_code == 200
    if not stream:
        result = response.json()
        result["choices"][0].pop("index", None)
        assert "error" not in result
        assert result["usage"]["total_tokens"] == 12
        return result
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.count("data: [DONE]") == 1
    events = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert not any("error" in event for event in events)
    message = {"role": "assistant", "content": None}
    modes = []
    finish = None
    for event in events:
        if "aistudio_full_reply" in event:
            modes.append(event["aistudio_full_reply"])
        for choice in event.get("choices", []):
            delta = choice["delta"]
            if "content" in delta:
                message["content"] = (message["content"] or "") + delta["content"]
            if "tool_calls" in delta:
                message["tool_calls"] = [
                    {key: value for key, value in call.items() if key != "index"}
                    for call in delta["tool_calls"]
                ]
            if choice["finish_reason"] is not None:
                finish = choice["finish_reason"]
    assert modes and all(mode == modes[0] for mode in modes)
    assert events[-1]["usage"]["total_tokens"] == 12
    return {
        "choices": [{"message": message, "finish_reason": finish}],
        "aistudio_full_reply": modes[0],
    }


def _failure(response, stream: bool) -> None:
    if not stream:
        assert response.status_code == 502
        assert "error" in response.json()
        return
    assert response.status_code == 200
    assert response.text.count("data: [DONE]") == 1
    events = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert any("error" in event for event in events)
    assert not any(
        choice.get("finish_reason") in ("stop", "tool_calls")
        or choice.get("delta", {}).get("tool_calls")
        for event in events
        for choice in event.get("choices", [])
    )


async def _exercise(project: Path) -> dict:
    import httpx
    from aistudio_api.api import routes_openai as routes
    from aistudio_api.api.app import app
    from aistudio_api.api.dependencies import get_client
    from aistudio_api.api.reply_cache import get_reply_cache
    from aistudio_api.application.chat_service import normalize_gemini_request
    from aistudio_api.infrastructure.gateway.client import AIStudioClient
    from aistudio_api.infrastructure.gateway.wire_spec import PartIndex

    assert Path(routes.__file__).resolve().is_relative_to(project)
    state = {"mode": "envelope", "tool_name": "context"}
    requests = []
    cases = 0
    wire_cases = 0
    original_handler = routes.handle_gemini_generate_content
    original_overrides = dict(app.dependency_overrides)
    client_stub = MagicMock(spec=AIStudioClient)

    async def synthetic_handler(model, request, client, *, stream, require_finish_reason):
        assert model == "gemini-4-argon"
        assert stream is False and require_finish_reason is True
        assert client is client_stub
        requests.append(request)
        envelope = request.tools[-1].functionDeclarations[-1]["name"]
        if state["mode"] == "plain":
            payload = _reply(envelope, {"content": ANSWER})
            payload["candidates"][0]["content"]["parts"] = [{"text": ANSWER}]
            return payload
        if state["mode"] in ("tool", "bad_arguments"):
            arguments = {"query": "synthetic lookup"}
            if state["mode"] == "bad_arguments":
                arguments = '{"query":'
            return _reply(state["tool_name"], arguments)
        payload = _reply(envelope, {"content": ANSWER})
        if state["mode"] == "missing_finish":
            payload["candidates"][0].pop("finishReason")
        return payload

    async def post(client, *, stream=False, name=None, messages=None, choice=None):
        nonlocal cases
        body = {
            "model": MODEL,
            "messages": messages or [{"role": "user", "content": "Synthetic request"}],
            "stream": stream,
            "stream_options": {"include_usage": True},
        }
        if name is not None:
            body["tools"] = [_tool(name)]
        if choice is not None:
            body["tool_choice"] = choice
        response = await client.post("/v1/chat/completions", json=body)
        cases += 1
        return response

    routes.handle_gemini_generate_content = synthetic_handler
    app.dependency_overrides[get_client] = lambda: client_stub
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://docker-probe.invalid"
        ) as client:
            for mode in ("envelope", "plain"):
                state["mode"] = mode
                for stream in (False, True):
                    result = _completion(await post(client, stream=stream), stream)
                    assert result["choices"][0] == {
                        "message": {"role": "assistant", "content": ANSWER},
                        "finish_reason": "stop",
                    }
                    assert result["aistudio_full_reply"] == {
                        "mode": "function_envelope" if mode == "envelope" else "plain_text_fallback"
                    }

            for name in ("context", "aistudio_full_reply"):
                state["tool_name"] = name
                for stream in (False, True):
                    state["mode"] = "tool"
                    before = len(requests)
                    first = _completion(await post(client, stream=stream, name=name), stream)
                    assert len(requests) == before + 1
                    choice = first["choices"][0]
                    assert choice["finish_reason"] == "tool_calls"
                    assert first["aistudio_full_reply"] == {"mode": "client_tool_call"}
                    tool_call = choice["message"]["tool_calls"][0]
                    assert tool_call["function"]["name"] == name
                    assert json.loads(tool_call["function"]["arguments"]) == {
                        "query": "synthetic lookup"
                    }
                    assert tool_call["id"].startswith("docker-probe-call--sig-")
                    messages = [
                        {"role": "user", "content": "Synthetic request"},
                        choice["message"],
                        {
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "content": "synthetic result",
                        },
                    ]
                    state["mode"] = "envelope"
                    final = _completion(
                        await post(client, stream=stream, name=name, messages=messages), stream
                    )
                    assert final["choices"][0]["message"]["content"] == ANSWER
                    assert final["choices"][0]["finish_reason"] == "stop"
                    native = normalize_gemini_request(requests[-1], "gemini-4-argon")
                    call_part = native.contents[1].parts[0].to_wire()
                    result_part = native.contents[2].parts[0].to_wire()
                    assert call_part[PartIndex.THOUGHT_SIGNATURE] == SIGNATURE
                    assert call_part[PartIndex.FUNCTION_CALL_ALT][0] == name
                    assert call_part[PartIndex.FUNCTION_CALL_ALT][2] == "docker-probe-call"
                    assert result_part[PartIndex.FUNCTION_RESPONSE_ALT][0] == name
                    assert result_part[PartIndex.FUNCTION_RESPONSE_ALT][2] == "docker-probe-call"
                    envelope = (
                        "aistudio_full_reply_1"
                        if name == "aistudio_full_reply"
                        else "aistudio_full_reply"
                    )
                    assert native.tool_config == [None, [2, [name, envelope]]]
                    wire_cases += 1

            state["tool_name"] = "context"
            for choice in (
                "auto",
                "none",
                "required",
                {"type": "function", "function": {"name": "context"}},
            ):
                state["mode"] = "envelope" if choice == "none" else "tool"
                allowed = ["aistudio_full_reply"] if choice == "none" else ["context"]
                if choice == "auto":
                    allowed.append("aistudio_full_reply")
                for stream in (False, True):
                    result = _completion(
                        await post(client, stream=stream, name="context", choice=choice), stream
                    )
                    assert result["choices"][0]["finish_reason"] == (
                        "stop" if choice == "none" else "tool_calls"
                    )
                    native = normalize_gemini_request(requests[-1], "gemini-4-argon")
                    assert native.tool_config == [None, [2, allowed]]
                    wire_cases += 1

            for mode in ("missing_finish", "bad_arguments"):
                state["mode"] = mode
                for stream in (False, True):
                    _failure(await post(client, stream=stream, name="context"), stream)

            for choice, mode in (("none", "tool"), ("required", "envelope")):
                state["mode"] = mode
                for stream in (False, True):
                    _failure(
                        await post(client, stream=stream, name="context", choice=choice), stream
                    )

            cache = get_reply_cache()
            app.dependency_overrides[routes._reply_cache_owner] = lambda: "docker-probe-owner"
            for stream in (False, True):
                await cache.configure(0)
                await cache.configure(1)
                await cache.save(
                    "docker-probe-owner",
                    {
                        "model": MODEL,
                        "choices": [
                            {
                                "message": {"content": "cached synthetic reply"},
                                "finish_reason": "stop",
                            }
                        ],
                    },
                )
                await cache.arm((await cache.status())["entries"][0]["id"])
                before = await cache.status()
                count = len(requests)
                state["mode"] = "tool"
                tool_turn = _completion(
                    await post(client, stream=stream, name="context", choice="required"), stream
                )
                assert tool_turn["choices"][0]["finish_reason"] == "tool_calls"
                assert len(requests) == count + 1
                assert await cache.status() == before

                # Simulate an old stored tool turn; it cannot be replayed as a
                # fresh answer or repeat a client action on a no-tools request.
                await cache.save("docker-probe-owner", {**tool_turn, "model": MODEL})
                await cache.arm((await cache.status())["entries"][0]["id"])
                state["mode"] = "envelope"
                count = len(requests)
                result = _completion(await post(client, stream=stream), stream)
                assert result["choices"][0]["message"]["content"] == ANSWER
                assert result["choices"][0]["finish_reason"] == "stop"
                assert len(requests) == count + 1
    finally:
        routes.handle_gemini_generate_content = original_handler
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original_overrides)

    return {
        "passed": True,
        "http_cases": cases,
        "native_wire_cases": wire_cases,
        "json_and_sse": True,
        "same_name_preset_tools": True,
        "signed_tool_result_roundtrip": True,
        "tool_choice_constraints": True,
        "invalid_replies_rejected": True,
        "tool_cache_replay_blocked": True,
        "provider": "synthetic",
        "external_network_attempts": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path("/app"))
    args = parser.parse_args()
    project = args.project.resolve()
    assert (project / "src/aistudio_api/api/routes_openai.py").is_file()
    logging.disable(logging.CRITICAL)
    attempts = []

    def reject_network(*args, **kwargs):
        attempts.append(True)
        raise RuntimeError("External network is disabled in the Docker runtime probe")

    with tempfile.TemporaryDirectory(prefix="ais-pg-docker-probe-") as directory:
        state = Path(directory)
        (state / "data/accounts").mkdir(parents=True)
        (state / "dumps").mkdir()
        config = state / "config.yaml"
        config.write_text(
            "api_keys: []\nlogging:\n  level: CRITICAL\n  dump_requests: false\n  dump_to_file: false\n",
            encoding="utf-8",
        )
        os.environ.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHON_DOTENV_DISABLED": "1",
                "AISTUDIO_CONFIG_FILE": str(config),
                "AISTUDIO_DATA_DIR": str(state / "data"),
                "AISTUDIO_ACCOUNTS_DIR": str(state / "data/accounts"),
                "AISTUDIO_STATS_FILE": str(state / "data/stats.json"),
                "AISTUDIO_ROTATOR_STATE_FILE": str(state / "data/rotator.json"),
                "AISTUDIO_AUTH_FILE": str(state / "data/auth.json"),
                "AISTUDIO_DUMP_DIR": str(state / "dumps"),
                "AISTUDIO_DUMP_REQUESTS": "0",
                "AISTUDIO_DUMP_FILE": "0",
                "AISTUDIO_DUMP_TO_FILE": "0",
                "AISTUDIO_PERSIST_STATS": "0",
                "AISTUDIO_PERSIST_ROTATOR": "0",
                "AISTUDIO_WEB_PASSWORD": "",
                "AISTUDIO_ADMIN_PASSWORD": "",
                "AISTUDIO_PROXY": "",
            }
        )
        sys.path.insert(0, str(project / "src"))

        async def guarded():
            # Initialize the event loop before blocking networking. On Windows
            # asyncio's internal socketpair uses a local TCP connection.
            with (
                patch.object(socket.socket, "connect", reject_network),
                patch.object(socket, "getaddrinfo", reject_network),
            ):
                return await _exercise(project)

        result = asyncio.run(guarded())
        assert not attempts
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
