"""Real process behavior when telemetry is disabled, partial, or unavailable."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .harness import PREFIX, Collector, WireServer
from .test_transport import PARENT, tool_spans

pytestmark = pytest.mark.integration


async def test_disabled_master_ignores_ambient_otel_and_stdio_remains_jsonrpc(tmp_path, collector):
    async with WireServer(
        tmp_path,
        collector,
        env={
            PREFIX + "ENABLED": "false",
            "OTEL_TRACES_EXPORTER": "otlp",
            "OTEL_PYTHON_TRACER_PROVIDER": "sentinel-unavailable-provider",
            "OTEL_EXPORTER_OTLP_ENDPOINT": collector.endpoint,
            "FASTMCP_TRANSPORT": "http",
        },
    ) as server:
        response = await server.call(meta={"traceparent": PARENT})
        assert response["result"]["structuredContent"]["status"] == "ok"
    assert collector.records == []


def test_required_session_cannot_be_forged(tmp_path, collector):
    fixture = WireServer(
        tmp_path,
        collector,
        env={
            "JUSTPEN_SESSION_ID": "",
            PREFIX + "REQUIRE_SESSION": "true",
            PREFIX + "RESOURCE_ATTRIBUTES": "justpen.session.id=private-forged-value",
        },
    )
    result = subprocess.run(
        [sys.executable, "-m", "justpen_knowledgebase_mcp"],
        env=fixture.env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode != 0
    assert "requires a valid JUSTPEN_SESSION_ID" in result.stderr
    assert "private-forged-value" not in result.stderr
    assert result.stdout == ""
    assert collector.records == []


@pytest.mark.parametrize("selected", ["TRACES", "LOGS", "METRICS"])
async def test_signal_subsets_work_on_the_wire(tmp_path, collector, selected):
    env = {PREFIX + name + "_ENABLED": str(name == selected).lower() for name in ("TRACES", "LOGS", "METRICS")}
    async with WireServer(tmp_path, collector, env=env) as server:
        await server.call(meta={"traceparent": PARENT})
    assert collector.signals() == {selected.lower()}
    if selected == "LOGS":
        terminal = [item for item in collector.logs() if item["body"] == "mcp.request.finished"]
        assert any(item["trace_id"] == "11" * 16 for item in terminal)


async def test_unreachable_collector_does_not_fail_http_or_delay_exit(tmp_path, collector):
    async with WireServer(
        tmp_path,
        collector,
        transport="http",
        env={PREFIX + "ENDPOINT": "http://127.0.0.1:1", PREFIX + "SHUTDOWN_TIMEOUT_MS": "100"},
    ) as server:
        response = await server.call(
            "kb_write",
            {"nodes": [{"type": "domain", "properties": {"name": "durable.example", "credential": "sentinel-secret"}}]},
        )
        assert response["result"]["structuredContent"]["status"] == "ok"
        identifier = response["result"]["structuredContent"]["data"]["nodes"][0]["id"]
        read = await server.call("kb_get", {"kind": "nodes", "ids": [identifier]})
        assert (
            read["result"]["structuredContent"]["data"]["records"][0]["properties"]["credential"] == "sentinel-secret"
        )
        stopped = time.monotonic()
    assert time.monotonic() - stopped < 2
    assert collector.records == []


async def test_failing_collector_and_tiny_queues_do_not_fail_tools_or_leak_response(tmp_path):
    collector = Collector(status=503)
    try:
        async with WireServer(
            tmp_path,
            collector,
            transport="http",
            env={
                PREFIX + "BSP_MAX_QUEUE_SIZE": "2",
                PREFIX + "BLRP_MAX_QUEUE_SIZE": "2",
                PREFIX + "TIMEOUT": "0.05",
                PREFIX + "SHUTDOWN_TIMEOUT_MS": "100",
            },
        ) as server:
            responses = await asyncio.gather(*(server.call(meta={"callId": f"call-{index}"}) for index in range(12)))
            assert all(response["result"]["structuredContent"]["status"] == "ok" for response in responses)
        assert collector.records
        assert "collector-sentinel-secret" not in server.stderr_path.read_text()
    finally:
        collector.close()


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_mcp_cancellation_emits_one_cancelled_terminal_event(tmp_path, collector, transport):
    marker = tmp_path / "tool-entered"
    async with WireServer(
        tmp_path,
        collector,
        transport=transport,
        fixture_server=True,
        env={"KB_TELEMETRY_TEST_MARKER": str(marker)},
    ) as server:
        task = asyncio.create_task(
            server.call(
                arguments={"secret": "block-value"},
                meta={"traceparent": PARENT, "callId": "cancelled-call"},
                request_id=777,
            )
        )
        try:
            deadline = time.monotonic() + 3
            for _attempt in range(300):
                if marker.exists() or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.01)
            assert marker.exists()
            await server.notify("notifications/cancelled", {"requestId": 777, "reason": "sentinel-secret"})
            terminal = []
            while time.monotonic() < deadline:
                terminal = [
                    item
                    for item in collector.logs()
                    if item["body"] == "mcp.request.finished"
                    and item["attributes"].get("gen_ai.tool.call.id") == "cancelled-call"
                ]
                if terminal:
                    break
                await asyncio.sleep(0.02)
            assert len(terminal) == 1
            assert terminal[0]["attributes"]["justpen.result.status"] == "cancelled"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert all(b"sentinel-secret" not in data for _name, data, _headers in collector.records)


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
async def test_signals_flush_before_actual_http_process_exit(tmp_path, collector, sig):
    async with WireServer(tmp_path, collector, transport="http") as server:
        await server.call(meta={"traceparent": PARENT})
        assert server.process is not None
        server.process.send_signal(sig)
        started = time.monotonic()
        await asyncio.wait_for(server.process.wait(), 2)
        assert server.process.returncode == 0
    assert time.monotonic() - started < 2
    assert tool_spans(collector)
    assert any(item["body"] == "mcp.server.stopped" for item in collector.logs())


def test_blocked_exporter_has_bounded_actual_process_exit():
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("blocked_exporter.py"))],
        capture_output=True,
        text=True,
        timeout=4,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started < 4
    assert json.loads(result.stdout)["shutdown_seconds"] < 0.6
    assert "budget expired" in result.stderr


def test_import_has_no_provider_or_worker_side_effect_even_when_enabled(collector):
    probe = """
import json, threading
from opentelemetry import trace
before = trace.get_tracer_provider()
from justpen_knowledgebase_mcp.telemetry import runtime
assert trace.get_tracer_provider() is before
print(json.dumps([thread.name for thread in threading.enumerate()]))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
        env={**os.environ, PREFIX + "ENABLED": "true", PREFIX + "ENDPOINT": collector.endpoint},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["MainThread"]
    assert collector.records == []
