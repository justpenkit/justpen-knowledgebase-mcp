"""Request-local correlation using W3C context and explicitly selected client IDs."""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal, cast

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.propagators import textmap
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.types import AttributeValue
from typing_extensions import override


class BoundedTraceContextPropagator(TraceContextTextMapPropagator):
    """Apply the same carrier size limits before native FastMCP span creation."""

    @override
    def extract(
        self,
        carrier: textmap.CarrierT,
        context: Context | None = None,
        getter: textmap.Getter[textmap.CarrierT] = textmap.default_getter,
    ) -> Context:
        """Delegate W3C parsing to the standard propagator after bounding inputs."""
        selected: dict[str, str] = {}
        for name in ("traceparent", "tracestate"):
            values = getter.get(carrier, name)
            if values and len(values) == 1 and 0 < len(values[0]) <= 512:
                selected[name] = values[0]
        return super().extract(selected, context=context)


@dataclass(frozen=True)
class IncomingContext:
    """Validated carrier state without retaining raw headers or metadata."""

    context: Context
    valid: bool
    invalid: bool


@dataclass
class RequestObservation:
    """One request's correlation and terminal outcome, independent of sampling."""

    method: str
    transport: Literal["stdio", "http"]
    started_clock: float
    attributes: dict[str, AttributeValue] = field(default_factory=dict[str, AttributeValue])
    outcome: Literal["success", "error", "cancelled"] = "success"
    error_type: str | None = None


current_observation: ContextVar[RequestObservation | None] = ContextVar("justpen_observation", default=None)
incoming_http: ContextVar[IncomingContext | None] = ContextVar("justpen_incoming_http", default=None)


def bounded_string(value: object) -> str | None:
    """Keep original scalar IDs; omit oversized or non-printable values."""
    return value if isinstance(value, str) and 1 <= len(value) <= 256 and value.isprintable() else None


def extract_carrier(carrier: Mapping[str, object]) -> IncomingContext:
    """Extract only W3C trace context onto a clean root, never baggage."""
    selected: dict[str, str] = {}
    invalid = False
    for name, limit in (("traceparent", 512), ("tracestate", 512)):
        if name not in carrier:
            continue
        value = carrier[name]
        if isinstance(value, str) and 0 < len(value) <= limit:
            selected[name] = value
        else:
            invalid = True
    context = BoundedTraceContextPropagator().extract(selected, context=Context())
    parent = trace.get_current_span(context).get_span_context()
    if "traceparent" in carrier and not parent.is_valid:
        invalid = True
    if selected.get("tracestate") and not parent.trace_state:
        invalid = True
    return IncomingContext(context, parent.is_valid, invalid)


def client_attributes(meta: Mapping[str, object], *, client_name: str | None = None) -> dict[str, AttributeValue]:
    """Normalize native client identifiers without conflating their meanings."""
    attrs: dict[str, AttributeValue] = {}
    if name := bounded_string(client_name):
        attrs["justpen.client.name"] = name
    codex = bounded_string(meta.get("callId"))
    claude = bounded_string(meta.get("claudecode/toolUseId"))
    if codex and claude and codex != claude:
        attrs["justpen.correlation.conflict"] = True
    elif call_id := codex or claude:
        attrs["gen_ai.tool.call.id"] = call_id
    nested = meta.get("x-codex-turn-metadata")
    turn: Mapping[str, object] = cast("Mapping[str, object]", nested) if isinstance(nested, Mapping) else {}
    outer_thread = bounded_string(meta.get("threadId"))
    inner_thread = bounded_string(turn.get("thread_id"))
    if outer_thread and inner_thread and outer_thread != inner_thread:
        attrs["justpen.correlation.conflict"] = True
    for field_name, value in (
        ("session", turn.get("session_id")),
        ("thread", inner_thread or outer_thread),
        ("turn", turn.get("turn_id")),
        ("item", meta.get("itemId")),
    ):
        if identifier := bounded_string(value):
            attrs[f"justpen.client.{field_name}.id"] = identifier
    return attrs


def capture_job_context() -> dict[str, str]:
    """Snapshot only bounded W3C context on the request thread before DB dispatch."""
    try:
        carrier: dict[str, str] = {}
        BoundedTraceContextPropagator().inject(carrier)
        incoming = extract_carrier(carrier)
        if not incoming.valid:
            return {}
        selected: dict[str, str] = {}
        BoundedTraceContextPropagator().inject(selected, context=incoming.context)
    except Exception:  # noqa: BLE001 — optional correlation cannot prevent durable admission
        return {}
    else:
        return selected


def job_links(carrier: object) -> list[trace.Link]:
    """Revalidate persisted untrusted context; old requests are links, never parents."""
    if not isinstance(carrier, dict):
        return []
    incoming = extract_carrier(cast("Mapping[str, object]", carrier))
    if not incoming.valid:
        return []
    return [trace.Link(trace.get_current_span(incoming.context).get_span_context())]
