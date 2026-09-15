"""Sanitize native FastMCP spans at the export boundary using public SDK types."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Link, Status, StatusCode
from typing_extensions import override

from .context import bounded_string
from .payloads import known_method, safe_attributes

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


def sanitize_span(span: ReadableSpan) -> ReadableSpan:
    """Keep correlation and timing while removing content, exception text and URIs."""
    attributes = safe_attributes(span.attributes or {})
    method = known_method(attributes.get("mcp.method.name"))
    name = method
    tool = attributes.get("gen_ai.tool.name")
    if method == "tools/call" and isinstance(tool, str):
        name = f"{method} {tool}"
    if operation := attributes.get("justpen.operation.kind"):
        name = f"job {operation}"
    events: list[Event] = []
    for event in span.events:
        if event.name != "exception":
            continue
        error_type = bounded_string((event.attributes or {}).get("exception.type"))
        if error_type is not None:
            events.append(Event("exception", {"exception.type": error_type}, timestamp=event.timestamp))
    status = StatusCode.UNSET if attributes.get("justpen.result.status") == "cancelled" else span.status.status_code
    return ReadableSpan(
        name=name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=attributes,
        events=events,
        links=[Link(link.context) for link in span.links],
        kind=span.kind,
        status=Status(status),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class SanitizingSpanExporter(SpanExporter):
    """Delegate only projected spans and keep failure diagnostics out of OTLP logs."""

    def __init__(self, delegate: SpanExporter) -> None:
        """Wrap an owned native OTLP exporter."""
        self.delegate = delegate

    @override
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Sanitize a batch before the delegate receives any span content."""
        try:
            result = self.delegate.export([sanitize_span(span) for span in spans])
        except Exception:  # noqa: BLE001 — export failures must not expose exception payloads or fail tools
            result = SpanExportResult.FAILURE
        if result != SpanExportResult.SUCCESS:
            logger.warning("Telemetry trace export failed; the batch may not have been delivered")
        return result

    @override
    def shutdown(self) -> None:
        """Close the owned exporter under the runtime's shutdown budget."""
        self.delegate.shutdown()

    @override
    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Forward the remaining flush budget."""
        return self.delegate.force_flush(timeout_millis)


class DependencyDiagnosticFilter(logging.Filter):
    """Sanitize dependency diagnostics on CLI-owned stderr handlers, even without OTLP."""

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        """Preserve severity and source, never untrusted message arguments or tracebacks."""
        if record.name.startswith(
            ("fastmcp", "mcp.", "opentelemetry.", "uvicorn", "httpx", "httpcore", "urllib3", "grpc")
        ):
            category = "error" if record.exc_info else "diagnostic"
            if record.exc_info and record.exc_info[0] is not None:
                error_type = record.exc_info[0].__name__
                if error_type in {
                    "ValueError",
                    "TypeError",
                    "RuntimeError",
                    "OSError",
                    "ValidationError",
                    "ToolError",
                    "NotFoundError",
                    "CancelledError",
                }:
                    category = error_type
            record.msg = "Dependency %s %s from %s"
            record.args = (record.levelname, category, record.name)
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


def protect_cli_diagnostics() -> None:
    """Attach after FastMCP import; CLI disables subsequent handler reconfiguration."""
    for logger_name in ("", "fastmcp"):
        for handler in logging.getLogger(logger_name).handlers:
            if not any(isinstance(item, DependencyDiagnosticFilter) for item in handler.filters):
                handler.addFilter(DependencyDiagnosticFilter())
