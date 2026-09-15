import pytest
from opentelemetry import trace

from justpen_knowledgebase_mcp.telemetry.context import client_attributes, extract_carrier

PARENT = "00-11111111111111111111111111111111-2222222222222222-01"


def test_remote_parent_and_tracestate_are_preserved_without_baggage():
    incoming = extract_carrier({"traceparent": PARENT, "tracestate": "vendor=fixture", "baggage": "secret=yes"})
    parent = trace.get_current_span(incoming.context).get_span_context()
    assert incoming.valid
    assert not incoming.invalid
    assert parent.is_remote
    assert parent.trace_id == int("11" * 16, 16)
    assert parent.span_id == int("22" * 8, 16)
    assert parent.trace_state.to_header() == "vendor=fixture"
    assert len(incoming.context) == 1


@pytest.mark.parametrize("value", ["", "invalid", "00-" + "0" * 32 + "-" + "0" * 16 + "-01", [], 42, "x" * 1025])
def test_invalid_parent_cannot_supply_context(value):
    incoming = extract_carrier({"traceparent": value})
    assert not incoming.valid
    assert incoming.invalid
    assert not trace.get_current_span(incoming.context).get_span_context().is_valid


def test_missing_parent_is_distinguished_from_invalid():
    assert not extract_carrier({}).invalid
    assert not extract_carrier({}).valid


def test_bad_tracestate_does_not_discard_valid_parent():
    incoming = extract_carrier({"traceparent": PARENT, "tracestate": "bad value"})
    assert incoming.valid
    assert incoming.invalid
    assert not trace.get_current_span(incoming.context).get_span_context().trace_state


def test_codex_native_ids_remain_separate_and_preserve_values():
    attrs = client_attributes(
        {
            "callId": "call_original",
            "threadId": "thread-a",
            "itemId": "item-a",
            "x-codex-turn-metadata": {"session_id": "native-session", "thread_id": "thread-a", "turn_id": "turn-a"},
            "justpen.session.id": "untrusted",
            "arguments": {"secret": "value"},
        },
        client_name="codex-mcp-client",
    )
    assert attrs == {
        "gen_ai.tool.call.id": "call_original",
        "justpen.client.session.id": "native-session",
        "justpen.client.thread.id": "thread-a",
        "justpen.client.turn.id": "turn-a",
        "justpen.client.item.id": "item-a",
        "justpen.client.name": "codex-mcp-client",
    }


def test_claude_does_not_invent_missing_native_ids_or_client_name():
    assert client_attributes({"claudecode/toolUseId": "toolu_original"}) == {"gen_ai.tool.call.id": "toolu_original"}
    assert client_attributes({"callId": {}, "itemId": "x" * 257, "x-codex-turn-metadata": "{}"}) == {}


def test_conflicting_metadata_does_not_merge_different_identifiers():
    attrs = client_attributes(
        {
            "callId": "codex",
            "claudecode/toolUseId": "claude",
            "threadId": "outer",
            "x-codex-turn-metadata": {"thread_id": "inner"},
        }
    )
    assert "gen_ai.tool.call.id" not in attrs
    assert attrs["justpen.client.thread.id"] == "inner"
    assert attrs["justpen.correlation.conflict"] is True
