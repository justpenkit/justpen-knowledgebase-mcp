"""Enrich FastMCP's native request span without creating new spans."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Literal, cast

from fastmcp.server.middleware import Middleware
from mcp.types import CallToolRequestParams
from opentelemetry import trace
from typing_extensions import override

from .context import (
    RequestObservation,
    bounded_string,
    client_attributes,
    current_observation,
    extract_carrier,
    incoming_http,
)
from .payloads import KNOWN_TOOLS, apply_tool_result, known_method, safe_attributes

if TYPE_CHECKING:
    from fastmcp.server.middleware import CallNext, MiddlewareContext
    from fastmcp.tools import ToolResult
    from opentelemetry.util.types import AttributeValue

    from .events import TelemetryEvents


def _request_attributes(context: MiddlewareContext[Any]) -> dict[str, AttributeValue]:
    meta: Mapping[str, object] = {}
    request_id: object = None
    client_name = None
    if (ctx := context.fastmcp_context) is not None:
        if (request := ctx.request_context) is not None:
            meta = request.meta or {}
            request_id = request.request_id
        try:
            params = ctx.session.client_params
            if params is not None:
                client_name = params.client_info.name
        except RuntimeError:
            pass
    attrs = client_attributes(meta, client_name=client_name)
    if request_id is not None and (identifier := bounded_string(str(request_id))):
        attrs["justpen.request.id"] = identifier
    metadata_context = extract_carrier(meta)
    http = incoming_http.get()
    attrs["justpen.trace.context.source"] = (
        "http" if http and http.valid else "meta" if metadata_context.valid else "new_root"
    )
    attrs["justpen.trace.context.invalid"] = metadata_context.invalid or bool(http and http.invalid)
    attrs["justpen.trace.context.conflict"] = bool(
        http
        and http.valid
        and metadata_context.valid
        and trace.get_current_span(http.context).get_span_context()
        != trace.get_current_span(metadata_context.context).get_span_context()
    )
    return attrs


class TelemetryMiddleware(Middleware):
    """Observe each request once and keep request-local state through cancellation."""

    def __init__(self, *, events: TelemetryEvents, transport: Literal["stdio", "http"]) -> None:
        """Bind process event sinks and the configured MCP transport."""
        self.events = events
        self.transport: Literal["stdio", "http"] = transport

    @override
    async def on_request(self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]) -> Any:
        """Own the observation and terminal event within the native server span."""
        observation = RequestObservation(
            known_method(context.method), self.transport, time.monotonic(), _request_attributes(context)
        )
        # Normal calls carry validated params; early failures carry raw params.
        if context.method == "tools/call":
            message = context.message
            name: object = None
            if isinstance(message, CallToolRequestParams):
                name = message.name
            elif isinstance(message, Mapping):
                name = cast("Mapping[object, object]", message).get("name")
            if isinstance(name, str) and name in KNOWN_TOOLS:
                observation.attributes["gen_ai.tool.name"] = name
        token = current_observation.set(observation)
        span = trace.get_current_span()
        try:
            with suppress(Exception):
                self.events.request_started(observation)
            return await call_next(context)
        except asyncio.CancelledError:
            observation.outcome = "cancelled"
            raise
        except Exception as error:
            observation.outcome = "error"
            observation.error_type = bounded_string(type(error).__name__) or "internal_error"
            raise
        finally:
            try:
                observation.attributes.update(
                    {
                        "mcp.method.name": observation.method,
                        "justpen.transport": self.transport,
                        "justpen.result.status": observation.outcome,
                    }
                )
                if observation.error_type is not None:
                    observation.attributes["error.type"] = observation.error_type
                with suppress(Exception):
                    span.set_attributes(safe_attributes(observation.attributes))
                    if observation.outcome == "error":
                        span.set_status(trace.StatusCode.ERROR)
                with suppress(Exception):
                    self.events.request_finished(observation)
            finally:
                current_observation.reset(token)

    @override
    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: CallNext[Any, ToolResult]) -> ToolResult:
        """Interpret only the final Knowledge base result envelope."""
        observation = current_observation.get()
        if observation is not None and context.message.name in KNOWN_TOOLS:
            observation.attributes["gen_ai.tool.name"] = context.message.name
        result = await call_next(context)
        if (observation := current_observation.get()) is not None:
            apply_tool_result(observation, result)
        return result
