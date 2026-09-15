"""Bounded durable context and detached job step instrumentation."""

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from opentelemetry import baggage, trace
from opentelemetry.context import Context, attach, detach
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from justpen_knowledgebase_mcp.telemetry import context
from justpen_knowledgebase_mcp.telemetry.events import TelemetryEvents
from justpen_knowledgebase_mcp.telemetry.export import SanitizingSpanExporter


def test_durable_context_uses_active_span_and_never_baggage():
    parent = context.extract_carrier(
        {"traceparent": "00-" + "11" * 16 + "-" + "22" * 8 + "-01", "tracestate": "vendor=test"}
    )
    token = attach(baggage.set_baggage("secret", "sentinel-secret", parent.context))
    try:
        carrier = context.capture_job_context()
    finally:
        detach(token)
    assert carrier == {"traceparent": "00-" + "11" * 16 + "-" + "22" * 8 + "-01", "tracestate": "vendor=test"}
    assert context.capture_job_context() == {}


@pytest.mark.parametrize(
    "carrier", [{}, {"traceparent": "bad"}, {"traceparent": "x" * 513}, {"traceparent": []}, "bad"]
)
def test_invalid_stored_context_has_no_job_link(carrier):
    assert context.job_links(carrier) == []


def test_job_step_is_new_root_linked_to_persisted_initiating_span(monkeypatch):
    memory = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource({"justpen.session.id": "canonical"}), shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(SanitizingSpanExporter(memory)))
    monkeypatch.setattr(context.trace, "get_tracer", provider.get_tracer)
    events = TelemetryEvents(logger_provider=None, meter_provider=None)
    with provider.get_tracer("test").start_as_current_span("initiator") as parent:
        stored = json.loads(json.dumps(context.capture_job_context()))
        initiating = parent.get_span_context()
    with events.job_step("ingest", stored):
        running = trace.get_current_span().get_span_context()
        assert running.trace_id != initiating.trace_id
    job = memory.get_finished_spans()[-1]
    assert job.name == "job ingest"
    assert job.parent is None
    assert job.links[0].context.trace_id == initiating.trace_id
    assert job.links[0].context.span_id == initiating.span_id
    assert job.attributes is not None
    assert job.attributes["justpen.operation.kind"] == "ingest"
    assert job.resource.attributes["justpen.session.id"] == "canonical"
    provider.shutdown()


def test_broken_job_telemetry_never_changes_operation(monkeypatch):
    monkeypatch.setattr(context.trace, "get_tracer", MagicMock(side_effect=ValueError("sentinel-secret")))
    events = TelemetryEvents(logger_provider=None, meter_provider=None)
    with events.job_step("ingest", {}):
        result = "committed"
    assert result == "committed"


def test_job_failure_is_propagated_without_exporting_exception(monkeypatch):
    memory = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource({}), shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(SanitizingSpanExporter(memory)))
    monkeypatch.setattr(context.trace, "get_tracer", provider.get_tracer)
    events = TelemetryEvents(logger_provider=None, meter_provider=None)
    with pytest.raises(ValueError, match="sentinel-secret"), events.job_step("reindex", {}):
        raise ValueError("sentinel-secret")
    job = memory.get_finished_spans()[-1]
    assert job.attributes is not None
    assert job.attributes["justpen.result.status"] == "error"
    assert "sentinel-secret" not in job.to_json()
    assert trace.get_current_span(Context()).get_span_context().is_valid is False
    provider.shutdown()


async def test_job_cancel_resets_ambient_context_and_records_cancelled(monkeypatch):

    memory = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource({}), shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(SanitizingSpanExporter(memory)))
    monkeypatch.setattr(context.trace, "get_tracer", provider.get_tracer)
    try:
        with provider.get_tracer("host").start_as_current_span("outer") as outer:
            with (
                pytest.raises(asyncio.CancelledError),
                TelemetryEvents(logger_provider=None, meter_provider=None).job_step("ingest", {}),
            ):
                raise asyncio.CancelledError
            assert trace.get_current_span().get_span_context() == outer.get_span_context()
        job = memory.get_finished_spans()[0]
        assert job.parent is None
        assert job.attributes is not None
        assert job.attributes["justpen.result.status"] == "cancelled"
        assert job.status.status_code == trace.StatusCode.UNSET
    finally:
        provider.shutdown()


def test_capture_failure_omits_context_without_disclosing_input(monkeypatch):
    monkeypatch.setattr(
        context.BoundedTraceContextPropagator, "inject", MagicMock(side_effect=ValueError("sentinel-secret"))
    )
    assert context.capture_job_context() == {}
