"""Knowledge base result projection and FastMCP middleware contracts."""

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools import ToolResult
from mcp.types import CallToolRequestParams
from opentelemetry import trace

from justpen_knowledgebase_mcp.app import create_app
from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.telemetry.context import RequestObservation, current_observation
from justpen_knowledgebase_mcp.telemetry.middleware import TelemetryMiddleware
from justpen_knowledgebase_mcp.telemetry.payloads import KNOWN_TOOLS, apply_tool_result


@pytest.mark.parametrize(
    ("payload", "is_error", "outcome", "error_type"),
    [
        ({"status": "ok", "data": {"code": "sentinel-secret"}}, False, "success", None),
        ({"status": "error", "error": "INVALID: sentinel-secret"}, False, "error", "INVALID"),
        ({"status": "error", "error": "IO_ERROR: sentinel-secret"}, False, "error", "IO_ERROR"),
        ({"status": "error", "error": "LIMIT: sentinel-secret"}, False, "error", "LIMIT"),
        ({"status": "error", "error": "UNKNOWN: sentinel-secret"}, False, "error", "tool_error"),
        ({"status": "ok"}, True, "error", "tool_error"),
    ],
)
def test_final_tool_envelope_sets_only_bounded_outcome(payload, is_error, outcome, error_type):
    observation = RequestObservation("tools/call", "stdio", time.monotonic())
    result = ToolResult(structured_content=payload, is_error=is_error)
    apply_tool_result(observation, result)
    assert observation.outcome == outcome
    assert observation.error_type == error_type
    assert "sentinel-secret" not in repr(observation)


async def test_known_tools_agree_with_registered_server():
    assert {
        tool.name for tool in await create_app(ServerConfig(workspace_dir=Path("/workspace"))).list_tools()
    } == KNOWN_TOOLS


async def test_request_metadata_is_projected_without_payload():
    events = MagicMock()
    middleware = TelemetryMiddleware(events=events, transport="stdio")
    fastmcp_context = MagicMock()
    fastmcp_context.request_context.meta = {
        "callId": "call-a",
        "arguments": {"secret": "sentinel-secret"},
    }
    fastmcp_context.request_context.request_id = 42
    fastmcp_context.session.client_params.client_info.name = "codex-mcp-client"
    await middleware.on_request(
        MiddlewareContext(method="tools/call", message={}, fastmcp_context=fastmcp_context),
        AsyncMock(return_value={}),
    )
    observation = events.request_finished.call_args.args[0]
    assert observation.attributes["justpen.request.id"] == "42"
    assert observation.attributes["gen_ai.tool.call.id"] == "call-a"
    assert observation.attributes["justpen.client.name"] == "codex-mcp-client"
    assert "sentinel-secret" not in repr(observation.attributes)


@pytest.mark.parametrize(("name", "expected"), [("kb_status", "kb_status"), ("sentinel-secret", None)])
@pytest.mark.parametrize("typed", [False, True])
async def test_started_event_uses_only_known_tool_name(name, expected, typed):
    started: list[dict[str, object]] = []
    events = MagicMock()
    events.request_started.side_effect = lambda observation: started.append(dict(observation.attributes))
    middleware = TelemetryMiddleware(events=events, transport="stdio")
    await middleware.on_request(
        MiddlewareContext(
            method="tools/call",
            message=(
                CallToolRequestParams(name=name, arguments={"secret": "sentinel-secret"})
                if typed
                else {"name": name, "arguments": {"secret": "sentinel-secret"}}
            ),
        ),
        AsyncMock(return_value={}),
    )
    assert len(started) == 1
    assert started[0].get("gen_ai.tool.name") == expected
    assert "sentinel-secret" not in repr(started[0])


@pytest.mark.parametrize(
    ("failure", "outcome"), [(ValueError("private"), "error"), (asyncio.CancelledError(), "cancelled")]
)
async def test_terminal_outcome_and_context_cleanup(failure, outcome):
    events = MagicMock()
    middleware = TelemetryMiddleware(events=events, transport="http")
    with pytest.raises(type(failure)):
        await middleware.on_request(MiddlewareContext(method="ping", message={}), AsyncMock(side_effect=failure))
    observation = events.request_finished.call_args.args[0]
    assert observation.outcome == outcome
    assert current_observation.get() is None
    assert trace.get_current_span().get_span_context().trace_id == 0


async def test_broken_events_cannot_replace_success_or_failure():
    events = MagicMock()
    events.request_started.side_effect = ValueError("telemetry-secret")
    events.request_finished.side_effect = ValueError("telemetry-secret")
    middleware = TelemetryMiddleware(events=events, transport="stdio")
    assert (
        await middleware.on_request(MiddlewareContext(method="ping", message={}), AsyncMock(return_value="ok")) == "ok"
    )
    with pytest.raises(RuntimeError, match="operation failed"):
        await middleware.on_request(
            MiddlewareContext(method="ping", message={}), AsyncMock(side_effect=RuntimeError("operation failed"))
        )
    assert current_observation.get() is None


@pytest.mark.parametrize("observed", [True, False])
@pytest.mark.parametrize("name", ["kb_write", "sentinel-secret"])
async def test_tool_observation_uses_allowlisted_name_and_bounded_error(observed, name):

    result = ToolResult(structured_content={"status": "error", "error": "CONFLICT: sentinel-secret"}, is_error=True)
    observation = RequestObservation("tools/call", "stdio", time.monotonic()) if observed else None
    token = current_observation.set(observation)
    try:
        middleware = TelemetryMiddleware(events=MagicMock(), transport="stdio")
        assert (
            await middleware.on_call_tool(
                MiddlewareContext(method="tools/call", message=SimpleNamespace(name=name)),
                AsyncMock(return_value=result),
            )
            is result
        )
        if observation is not None:
            assert observation.outcome == "error"
            assert observation.error_type == "CONFLICT"
            assert observation.attributes.get("gen_ai.tool.name") == ("kb_write" if name == "kb_write" else None)
            assert "sentinel-secret" not in repr(observation)
    finally:
        current_observation.reset(token)
