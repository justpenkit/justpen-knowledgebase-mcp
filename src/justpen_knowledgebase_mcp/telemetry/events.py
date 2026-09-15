"""Structured request events and two low-cardinality metrics on owned providers."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry._logs import SeverityNumber
from opentelemetry.context import Context, attach, detach

from .context import RequestObservation, job_links
from .payloads import KNOWN_TOOLS, known_method, safe_attributes

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.util.types import AttributeValue


logger = logging.getLogger(__name__)
_LIFECYCLE_EVENTS = frozenset({"mcp.server.ready", "mcp.server.stopping", "mcp.server.stopped", "mcp.server.failed"})


class TelemetryEvents:
    """Emit fixed operational records without forwarding application logging."""

    def __init__(self, *, logger_provider: LoggerProvider | None, meter_provider: MeterProvider | None) -> None:
        """Use injected providers; a disabled signal performs no SDK work."""
        self._logger = logger_provider.get_logger("justpen_knowledgebase_mcp") if logger_provider is not None else None
        meter = meter_provider.get_meter("justpen_knowledgebase_mcp") if meter_provider is not None else None
        self._count = meter.create_counter("justpen.mcp.requests", unit="{request}") if meter is not None else None
        self._job_count = meter.create_counter("justpen.kb.job.steps", unit="{step}") if meter is not None else None
        self._job_duration = meter.create_histogram("justpen.kb.job.duration", unit="s") if meter is not None else None
        self._duration = meter.create_histogram("justpen.mcp.request.duration", unit="s") if meter is not None else None

    def _emit(self, event: str, attributes: Mapping[str, object]) -> None:
        if self._logger is None:
            return
        try:
            self._logger.emit(
                body=event,
                event_name=event,
                severity_number=SeverityNumber.INFO,
                attributes=safe_attributes(attributes),
            )
        except Exception:  # noqa: BLE001 — telemetry must not fail tools or disclose exception payloads
            # Never feed exporter failures back into the pipeline that failed.
            logger.warning("Telemetry event emission failed")

    def request_started(self, observation: RequestObservation) -> None:
        """Record request entry with active trace context, regardless of sampling."""
        self._emit(
            "mcp.request.started",
            {
                **observation.attributes,
                "mcp.method.name": known_method(observation.method),
                "justpen.transport": observation.transport,
            },
        )

    def request_finished(self, observation: RequestObservation) -> None:
        """Record one terminal event and duration/count without dynamic metric labels."""
        labels = {
            "mcp.method.name": known_method(observation.method),
            "justpen.transport": observation.transport,
            "justpen.result.status": observation.outcome,
        }
        tool = observation.attributes.get("gen_ai.tool.name")
        if isinstance(tool, str) and tool in KNOWN_TOOLS:
            labels["gen_ai.tool.name"] = tool
        duration = max(0, time.monotonic() - observation.started_clock)
        attributes: dict[str, object] = {**observation.attributes, **labels, "justpen.duration.seconds": duration}
        if observation.error_type is not None:
            attributes["error.type"] = observation.error_type
        self._emit("mcp.request.finished", attributes)
        try:
            if self._count is not None:
                self._count.add(1, labels)
            if self._duration is not None:
                self._duration.record(duration, labels)
        except Exception:  # noqa: BLE001 — optional metrics must not change the MCP result
            logger.warning("Telemetry metric recording failed")

    @contextmanager
    def job_step(self, kind: str, carrier: object) -> Generator[RequestObservation]:
        """Time one owned durable step in a new trace linked to its admission span."""
        kind = kind if kind in {"ingest", "reindex", "delete"} else "unknown"
        observation = RequestObservation("job", "stdio", time.monotonic(), {"justpen.operation.kind": kind})
        span = None
        token = None
        with suppress(Exception):
            span = trace.get_tracer("justpen_knowledgebase_mcp").start_span(
                "job " + kind, context=Context(), links=job_links(carrier), attributes=observation.attributes
            )
            token = attach(trace.set_span_in_context(span, Context()))
            self._emit("kb.job.step.started", observation.attributes)
        try:
            yield observation
        except asyncio.CancelledError:
            observation.outcome = "cancelled"
            raise
        except Exception:
            observation.outcome = "error"
            raise
        finally:
            labels = {"justpen.operation.kind": kind, "justpen.result.status": observation.outcome}
            duration = max(0, time.monotonic() - observation.started_clock)
            with suppress(Exception):
                self._emit("kb.job.step.finished", {**labels, "justpen.duration.seconds": duration})
                if self._job_count is not None:
                    self._job_count.add(1, labels)
                if self._job_duration is not None:
                    self._job_duration.record(duration, labels)
            if token is not None:
                detach(token)
            if span is not None:
                with suppress(Exception):
                    span.set_attributes(labels)
                    if observation.outcome == "error":
                        span.set_status(trace.StatusCode.ERROR)
                    span.end()

    def lifecycle(self, event: str, attributes: Mapping[str, AttributeValue]) -> None:
        """Emit only known lifecycle event names using the same process resource."""
        if event in _LIFECYCLE_EVENTS:
            self._emit(event, attributes)
