"""Full-reply tool policy and signed client history reach the native wire intact."""

from collections import OrderedDict

import pytest

from aistudio_api.api import routes_openai as routes
from aistudio_api.api.schemas import OpenAIChatCompletionRequest
from aistudio_api.application.anti_truncation import (
    tool_policy,
    validate_selection,
    wrap_request,
)
from aistudio_api.application.chat_service import normalize_gemini_request
from aistudio_api.infrastructure.gateway.wire_spec import PartIndex, SchemaIndex

MODEL = "gemini-4-argon[防截断]"


def _declaration(name):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Synthetic client lookup",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }


@pytest.mark.parametrize("name", ["context", "aistudio_full_reply"])
@pytest.mark.parametrize("choice", [None, "auto", "none", "required", "named"])
def test_serial_full_reply_declarations_and_choice_survive_native_encoding(
    name, choice
):
    client_choice = (
        {"type": "function", "function": {"name": "calculate"}}
        if choice == "named"
        else choice
    )
    request = OpenAIChatCompletionRequest(
        model=MODEL,
        messages=[{"role": "user", "content": "Synthetic lookup request"}],
        tools=[_declaration(name), _declaration("calculate")],
        tool_choice=client_choice,
    )
    validate_selection(request)
    policy = tool_policy(request)
    wrapped = wrap_request(routes.openai_request_to_gemini(request), policy)
    native = normalize_gemini_request(wrapped, "gemini-4-argon")

    # One wire declaration block contains both unchanged client functions and
    # the independently named private envelope; no native tool replaces them.
    assert native.tools is not None
    assert len(native.tools) == 1
    assert native.tools[0][0] is None
    declarations = native.tools[0][1]
    assert isinstance(declarations, list)
    envelope_name = (
        "aistudio_full_reply_1"
        if name == "aistudio_full_reply"
        else "aistudio_full_reply"
    )
    assert [declaration[0] for declaration in declarations] == [
        name,
        "calculate",
        envelope_name,
    ]
    for declaration in declarations[:2]:
        assert declaration[1] == "Synthetic client lookup"
        schema = declaration[2]
        assert isinstance(schema, list)
        assert schema[0] == 6  # Native object schema.
        assert schema[SchemaIndex.PROPERTIES] == [["query", [1]]]
        assert schema[SchemaIndex.REQUIRED] == ["query"]
    envelope_schema = declarations[2][2]
    assert isinstance(envelope_schema, list)
    assert envelope_schema[SchemaIndex.PROPERTIES] == [["content", [1]]]
    assert envelope_schema[SchemaIndex.REQUIRED] == ["content"]

    allowed = {
        None: [name, "calculate", envelope_name],
        "auto": [name, "calculate", envelope_name],
        "none": [envelope_name],
        "required": [name, "calculate"],
        "named": ["calculate"],
    }[choice]
    assert native.tool_config == [None, [2, allowed]]  # ANY + exact allowed names.


@pytest.mark.parametrize("name", ["context", "aistudio_full_reply"])
@pytest.mark.parametrize("retain_tools", [False, True])
def test_signed_client_result_roundtrip_reaches_native_wire_without_signature_cache(
    monkeypatch, name, retain_tools
):
    # The signature travels in the externally returned ID. A stale process-wide
    # entry with the same raw ID must neither replace it nor be overwritten.
    signatures = OrderedDict({"wire-call": "stale-signature"})
    monkeypatch.setattr(routes, "_TOOL_CALL_SIGNATURES", signatures)
    initial = OpenAIChatCompletionRequest(
        model=MODEL,
        messages=[{"role": "user", "content": "Synthetic lookup"}],
        tools=[_declaration(name)],
    )
    completion = routes.gemini_response_to_openai(
        {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    "name": name,
                                    "args": {"query": "synthetic lookup"},
                                    "id": "wire-call",
                                },
                                "thoughtSignature": "c3ludGhldGljLXNpZ25hdHVyZQ==",
                            }
                        ],
                    },
                }
            ],
        },
        model=MODEL,
        full_reply_policy=tool_policy(initial),
    )
    choices = completion["choices"]
    assert isinstance(choices, list)
    message = choices[0]["message"]
    call = message["tool_calls"][0]
    assert call["id"] != "wire-call"
    assert choices[0]["finish_reason"] == "tool_calls"
    followup = OpenAIChatCompletionRequest(
        model=MODEL,
        messages=[
            message,
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": "synthetic tool result",
            },
        ],
        tools=[_declaration(name)] if retain_tools else None,
        tool_choice="auto",
    )
    validate_selection(followup)
    policy = tool_policy(followup)
    native = normalize_gemini_request(
        wrap_request(routes.openai_request_to_gemini(followup), policy),
        "gemini-4-argon",
    )
    assert signatures == {"wire-call": "stale-signature"}
    assert native.contents[0].role == "model"
    assert native.contents[1].role == "user"
    native_call = native.contents[0].parts[0].to_wire()
    assert native_call[PartIndex.FUNCTION_CALL_ALT] == [
        name,
        [[["query", [None, None, "synthetic lookup"]]]],
        "wire-call",
    ]
    assert native_call[PartIndex.THOUGHT_SIGNATURE] == "c3ludGhldGljLXNpZ25hdHVyZQ=="
    native_result = native.contents[1].parts[0].to_wire()
    assert native_result[PartIndex.FUNCTION_RESPONSE_ALT] == [
        name,
        [[["result", [None, None, "synthetic tool result"]]]],
        "wire-call",
    ]
    # A formerly declared preset name remains distinct from the private
    # envelope even if this follow-up no longer provides client declarations.
    envelope_name = (
        "aistudio_full_reply_1"
        if name == "aistudio_full_reply"
        else "aistudio_full_reply"
    )
    assert native.tools is not None
    declarations = native.tools[0][1]
    assert isinstance(declarations, list)
    assert declarations[-1][0] == envelope_name
    assert native.tool_config == [
        None,
        [2, [*([name] if retain_tools else []), envelope_name]],
    ]
