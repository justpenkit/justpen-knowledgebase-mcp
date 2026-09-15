"""Bounded Knowledge base telemetry projections; tool content is never exported."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from ..errors import VALID_ERROR_TYPES
from .context import bounded_string

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastmcp.tools import ToolResult
    from opentelemetry.util.types import AttributeValue

    from .context import RequestObservation

KNOWN_TOOLS = frozenset(
    {
        "kb_status",
        "kb_types",
        "kb_write",
        "kb_get",
        "kb_search",
        "kb_neighbors",
        "kb_delete",
        "kb_ingest_evidence",
        "kb_read_evidence",
        "kb_reindex",
        "kb_jobs",
    }
)

ERROR_TYPES = VALID_ERROR_TYPES
METHODS = frozenset(
    {
        "initialize",
        "ping",
        "tools/call",
        "tools/list",
        "resources/read",
        "resources/list",
        "resources/templates/list",
        "resources/subscribe",
        "resources/unsubscribe",
        "prompts/get",
        "prompts/list",
        "completion/complete",
        "logging/setLevel",
        "tasks/get",
        "tasks/result",
        "tasks/list",
        "tasks/cancel",
        "server/discover",
    }
)
ATTRIBUTE_NAMES = frozenset(
    {
        "mcp.method.name",
        "mcp.protocol.version",
        "mcp.session.id",
        "fastmcp.server.name",
        "gen_ai.tool.name",
        "gen_ai.tool.call.id",
        "justpen.client.name",
        "justpen.client.session.id",
        "justpen.client.thread.id",
        "justpen.client.turn.id",
        "justpen.client.item.id",
        "justpen.correlation.conflict",
        "justpen.request.id",
        "justpen.trace.context.source",
        "justpen.trace.context.invalid",
        "justpen.trace.context.conflict",
        "justpen.transport",
        "justpen.result.status",
        "error.type",
        "justpen.operation.kind",
        "justpen.duration.seconds",
    }
)


def known_method(value: object) -> str:
    """Bound request method cardinality, including malformed or unsupported names."""
    return value if isinstance(value, str) and value in METHODS else "unknown"


def _safe_value(name: str, value: object) -> AttributeValue | None:
    if name == "justpen.duration.seconds":
        if isinstance(value, float) and math.isfinite(value) and value >= 0:
            return value
        return None
    if name == "mcp.method.name":
        return known_method(value)
    if name == "justpen.operation.kind":
        return value if value in ("ingest", "reindex", "delete") else "unknown"
    if name == "gen_ai.tool.name":
        return value if isinstance(value, str) and value in KNOWN_TOOLS else None
    if isinstance(value, bool):
        return value
    return bounded_string(value)


def safe_attributes(attributes: Mapping[str, object]) -> dict[str, AttributeValue]:
    """Project allowlisted scalar fields without user content or unbounded labels."""
    result: dict[str, AttributeValue] = {}
    for name, value in attributes.items():
        if name in ATTRIBUTE_NAMES and (selected := _safe_value(name, value)) is not None:
            result[name] = selected
    return result


def apply_tool_result(observation: RequestObservation, result: ToolResult) -> None:
    """Project only the bounded outcome and exact Knowledge base error prefix."""
    payload = result.structured_content or {}
    if result.is_error or payload.get("status") == "error":
        observation.outcome = "error"
        value = payload.get("error")
        code = value.partition(":")[0] if isinstance(value, str) else ""
        observation.error_type = code if code in ERROR_TYPES else "tool_error"
