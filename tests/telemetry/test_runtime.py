import asyncio
import os
import threading
import time
from unittest.mock import MagicMock

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from justpen_knowledgebase_mcp.telemetry import runtime
from justpen_knowledgebase_mcp.telemetry.config import read_config
from justpen_knowledgebase_mcp.telemetry.events import TelemetryEvents

PREFIX = "JUSTPEN_KNOWLEDGEBASE_OTEL_"


async def test_disabled_runtime_does_not_install_or_modify_sdk(monkeypatch):
    install = MagicMock()
    monkeypatch.setattr(runtime.trace, "set_tracer_provider", install)
    before = dict(os.environ)
    handle = runtime.initialize(read_config({}), service_version="test")
    assert not handle.enabled
    assert handle.asgi_middleware() == []
    await handle.shutdown()
    await handle.shutdown()
    assert dict(os.environ) == before
    install.assert_not_called()


@pytest.mark.parametrize("protocol", ["http/protobuf", "grpc"])
@pytest.mark.parametrize("signal", ["TRACES", "LOGS", "METRICS", "ALL"])
async def test_runtime_builds_selected_signals_with_one_resource(monkeypatch, protocol, signal):
    # Inject exporters, keeping real SDK providers without replacing global SDK state.
    install = MagicMock()
    monkeypatch.setattr(runtime.trace, "set_tracer_provider", install)
    monkeypatch.setattr(runtime.propagate, "set_global_textmap", MagicMock())
    monkeypatch.setattr(runtime, "configure_sdk_environment", MagicMock())
    traces, logs = InMemorySpanExporter(), InMemoryLogRecordExporter()
    factories = {}
    for name, exporter in (("trace", traces), ("log", logs), ("metric", MagicMock())):
        factories[name] = MagicMock(return_value=exporter)
        monkeypatch.setattr(runtime, f"_{name}_exporter", factories[name])
    monkeypatch.setattr(runtime, "PeriodicExportingMetricReader", lambda exporter: InMemoryMetricReader())
    env = {PREFIX + "ENABLED": "true", PREFIX + "PROTOCOL": protocol, "JUSTPEN_SESSION_ID": "pentest-a"}
    env.update(
        {PREFIX + name + "_ENABLED": str(signal in {name, "ALL"}).lower() for name in ("TRACES", "LOGS", "METRICS")}
    )
    handle = runtime.initialize(read_config(env), service_version="test")
    assert handle.enabled
    assert len(handle.asgi_middleware()) == 1
    for name, setting in (("trace", "TRACES"), ("log", "LOGS"), ("metric", "METRICS")):
        if signal in {setting, "ALL"}:
            factories[name].assert_called_once_with(protocol)
        else:
            factories[name].assert_not_called()
    tracer_provider = install.call_args.args[0]
    assert tracer_provider.resource.attributes["justpen.session.id"] == "pentest-a"
    await handle.shutdown()


@pytest.mark.parametrize("mode", ["off", "propagation_only"])
def test_native_fastmcp_mode_conflict_is_explicit(monkeypatch, mode):
    monkeypatch.setattr(runtime.fastmcp.settings, "telemetry_mode", mode)
    with pytest.raises(ValueError, match="FASTMCP_TELEMETRY_MODE"):
        runtime.initialize(
            read_config({PREFIX + "ENABLED": "true", "JUSTPEN_SESSION_ID": "test"}), service_version="test"
        )


async def test_shutdown_is_bounded_idempotent_and_other_signals_still_close(caplog):
    release = threading.Event()
    blocked, healthy = MagicMock(), MagicMock()
    blocked.force_flush.side_effect = lambda **kwargs: release.wait(5)
    handle = runtime.TelemetryRuntime(
        enabled=True,
        events=TelemetryEvents(logger_provider=None, meter_provider=None),
        providers=[blocked, healthy],
        shutdown_timeout_ms=50,
    )
    started = time.monotonic()
    try:
        await asyncio.gather(handle.shutdown(), handle.shutdown())
        assert time.monotonic() - started < 0.5
        healthy.shutdown.assert_called_once()
        assert "budget" in caplog.text
    finally:
        release.set()


async def test_flush_failure_does_not_skip_shutdown_or_leak_error(caplog):
    broken = MagicMock()
    broken.force_flush.side_effect = ValueError("sentinel-secret")
    handle = runtime.TelemetryRuntime(
        enabled=True,
        events=TelemetryEvents(logger_provider=None, meter_provider=None),
        providers=[broken],
        shutdown_timeout_ms=100,
    )
    await handle.shutdown()
    broken.shutdown.assert_called_once()
    assert "sentinel-secret" not in caplog.text


@pytest.mark.parametrize("stage", ["logs", "reader", "meter"])
async def test_partial_setup_closes_all_acquired_resources(monkeypatch, stage):
    monkeypatch.setattr(runtime, "configure_sdk_environment", MagicMock())
    traces, logs, metrics = MagicMock(), MagicMock(), MagicMock()
    loop = asyncio.get_running_loop()
    closed = [asyncio.Event() for _ in range(3)]
    for exporter, event in zip((traces, logs, metrics), closed, strict=True):
        exporter.shutdown.side_effect = lambda *args, event=event, **kwargs: loop.call_soon_threadsafe(event.set)
    monkeypatch.setattr(runtime, "_trace_exporter", lambda protocol: traces)
    monkeypatch.setattr(runtime, "_log_exporter", lambda protocol: logs)
    monkeypatch.setattr(runtime, "_metric_exporter", lambda protocol: metrics)
    failure = MagicMock(side_effect=ValueError("sentinel-secret"))
    if stage == "logs":
        monkeypatch.setattr(runtime, "_log_exporter", failure)
    elif stage == "reader":
        monkeypatch.setattr(runtime, "PeriodicExportingMetricReader", failure)
    else:
        monkeypatch.setattr(runtime, "MeterProvider", failure)
    with pytest.raises(ValueError, match="Knowledge base telemetry initialization failed") as error:
        runtime.initialize(
            read_config({PREFIX + "ENABLED": "true", "JUSTPEN_SESSION_ID": "test"}), service_version="test"
        )
    assert "sentinel-secret" not in str(error.value)
    await asyncio.wait_for(closed[0].wait(), 1)
    traces.shutdown.assert_called_once()
    if stage != "logs":
        await asyncio.wait_for(asyncio.gather(closed[1].wait(), closed[2].wait()), 1)
        logs.shutdown.assert_called_once()
        metrics.shutdown.assert_called_once()


def test_enabled_without_signals_still_requires_session():
    config = read_config(
        {PREFIX + "ENABLED": "true", **{PREFIX + s + "_ENABLED": "false" for s in ("TRACES", "LOGS", "METRICS")}}
    )
    with pytest.raises(ValueError, match="JUSTPEN_SESSION_ID"):
        runtime.initialize(config, service_version="test")


async def test_cancelled_shutdown_caller_does_not_abandon_cleanup():
    release = threading.Event()
    entered = threading.Event()
    provider = MagicMock()

    def flush(**kwargs):
        entered.set()
        return release.wait(1)

    provider.force_flush.side_effect = flush
    handle = runtime.TelemetryRuntime(
        enabled=True,
        events=TelemetryEvents(logger_provider=None, meter_provider=None),
        providers=[provider],
        shutdown_timeout_ms=1000,
    )
    caller = asyncio.create_task(handle.shutdown())
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        assert entered.is_set()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        release.set()
        await handle.shutdown()
        provider.shutdown.assert_called_once()
    finally:
        release.set()


async def test_shutdown_failure_is_private_and_other_providers_close(caplog):
    failed, healthy = MagicMock(), MagicMock()
    failed.shutdown.side_effect = RuntimeError("sentinel-secret")
    failed.force_flush.return_value = False
    handle = runtime.TelemetryRuntime(
        enabled=True,
        events=TelemetryEvents(logger_provider=None, meter_provider=None),
        providers=[failed, healthy],
        shutdown_timeout_ms=1000,
    )
    await handle.shutdown()
    healthy.shutdown.assert_called_once()
    assert "sentinel-secret" not in caplog.text
    assert "shutdown failed" in caplog.text
