"""CLI-owned SDK setup, isolated environment and bounded exporter shutdown."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import fastmcp
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter as GrpcLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter as GrpcMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter as GrpcSpanExporter
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter as HttpLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter as HttpMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter as HttpSpanExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from starlette.middleware import Middleware

from .config import configure_sdk_environment
from .context import BoundedTraceContextPropagator
from .events import TelemetryEvents
from .export import SanitizingSpanExporter
from .http import HttpTraceContextMiddleware
from .resource import build_resource

if TYPE_CHECKING:
    from opentelemetry.sdk._logs.export import LogRecordExporter
    from opentelemetry.sdk.metrics.export import MetricExporter
    from opentelemetry.sdk.trace.export import SpanExporter

    from .config import TelemetryConfig

logger = logging.getLogger(__name__)


class Provider(Protocol):
    """Common public lifecycle of the three owned SDK providers."""

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Flush with the remaining budget."""
        ...

    def shutdown(self) -> None:
        """Close provider resources."""
        ...


def _trace_exporter(protocol: str) -> SpanExporter:
    return GrpcSpanExporter() if protocol == "grpc" else HttpSpanExporter()


def _log_exporter(protocol: str) -> LogRecordExporter:
    return GrpcLogExporter() if protocol == "grpc" else HttpLogExporter()


def _metric_exporter(protocol: str) -> MetricExporter:
    return GrpcMetricExporter() if protocol == "grpc" else HttpMetricExporter()


def _close_provider(provider: Provider, deadline: float, done: threading.Event) -> None:
    try:
        remaining = max(0, int((deadline - time.monotonic()) * 1000))
        if remaining and not provider.force_flush(timeout_millis=remaining):
            logger.warning("Telemetry flush did not complete")
    except Exception:  # noqa: BLE001 — best-effort cleanup must preserve the server's original error
        logger.warning("Telemetry flush failed")
    finally:
        try:
            provider.shutdown()
        except Exception:  # noqa: BLE001 — one signal's cleanup cannot prevent the others from closing
            logger.warning("Telemetry provider shutdown failed")
        finally:
            done.set()


def _start_cleanup(providers: list[Provider], deadline: float) -> list[threading.Event]:
    completed: list[threading.Event] = []
    for provider in providers:
        done = threading.Event()
        threading.Thread(
            target=_close_provider, args=(provider, deadline, done), name="telemetry-cleanup", daemon=True
        ).start()
        completed.append(done)
    return completed


@dataclass
class TelemetryRuntime:
    """Own provider lifetime without allowing exporters to hold process exit open."""

    enabled: bool
    events: TelemetryEvents
    providers: list[Provider] = field(default_factory=list[Provider], repr=False)
    shutdown_timeout_ms: int = 5000
    _shutdown_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    def asgi_middleware(self) -> list[Middleware]:
        """Return the public HTTP adapter only when telemetry is enabled."""
        return [Middleware(HttpTraceContextMiddleware)] if self.enabled else []

    async def _shutdown(self) -> None:
        deadline = time.monotonic() + self.shutdown_timeout_ms / 1000
        completed = _start_cleanup(self.providers, deadline)
        while not all(item.is_set() for item in completed):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("Telemetry shutdown budget expired; remaining delivery is not guaranteed")
                return
            await asyncio.sleep(min(0.01, remaining))

    async def shutdown(self) -> None:
        """Flush/close once, sharing one bounded task across concurrent callers."""
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._shutdown(), name="telemetry-shutdown")
        await asyncio.shield(self._shutdown_task)


def initialize(config: TelemetryConfig, *, service_version: str) -> TelemetryRuntime:
    """Initialize owned providers once in the CLI, never on module import."""
    handle = TelemetryRuntime(
        enabled=config.enabled,
        events=TelemetryEvents(logger_provider=None, meter_provider=None),
        shutdown_timeout_ms=config.shutdown_timeout_ms,
    )
    if config.export_enabled and config.session_id is None:
        raise ValueError("CONFIGURATION: enabled telemetry requires a valid JUSTPEN_SESSION_ID")
    if not config.enabled:
        return handle
    if fastmcp.settings.telemetry_mode != "native":
        raise ValueError("Enabled Knowledge base telemetry requires FASTMCP_TELEMETRY_MODE=native")
    resource = build_resource(config, service_version=service_version)
    configure_sdk_environment(config)
    try:
        tracer_provider = TracerProvider(resource=resource, shutdown_on_exit=False)
        handle.providers.append(tracer_provider)
        if config.traces.exporter == "otlp":
            span_exporter = SanitizingSpanExporter(_trace_exporter(config.traces.protocol))
            handle.providers.append(span_exporter)
            span_processor = BatchSpanProcessor(span_exporter)
            handle.providers[-1] = span_processor
            tracer_provider.add_span_processor(span_processor)
            handle.providers.pop()
        logger_provider = None
        if config.logs.exporter == "otlp":
            logger_provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
            handle.providers.append(logger_provider)
            log_exporter = _log_exporter(config.logs.protocol)
            handle.providers.append(log_exporter)
            log_processor = BatchLogRecordProcessor(log_exporter)
            handle.providers[-1] = log_processor
            logger_provider.add_log_record_processor(log_processor)
            handle.providers.pop()
        meter_provider = None
        if config.metrics.exporter == "otlp":
            metric_exporter = _metric_exporter(config.metrics.protocol)
            handle.providers.append(metric_exporter)
            reader = PeriodicExportingMetricReader(metric_exporter)
            handle.providers[-1] = reader
            meter_provider = MeterProvider(resource=resource, metric_readers=[reader], shutdown_on_exit=False)
            handle.providers[-1] = meter_provider
        handle.events = TelemetryEvents(logger_provider=logger_provider, meter_provider=meter_provider)
        propagate.set_global_textmap(BoundedTraceContextPropagator())
        trace.set_tracer_provider(tracer_provider)
    except Exception:  # noqa: BLE001 — failed SDK setup must close owned resources without leaking credentials
        _start_cleanup(handle.providers, time.monotonic() + config.shutdown_timeout_ms / 1000)
        raise ValueError(
            "Knowledge base telemetry initialization failed; check JUSTPEN_KNOWLEDGEBASE_OTEL_* settings"
        ) from None
    return handle
