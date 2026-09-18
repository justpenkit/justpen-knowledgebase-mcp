"""Real Utility MCP transports and OTLP payload acceptance."""

import asyncio
import json

import apsw
import pytest

from .harness import PREFIX, WireServer

pytestmark = pytest.mark.integration


PARENT = f"00-{'11' * 16}-{'22' * 8}-01"


OTHER = f"00-{'33' * 16}-{'44' * 8}-01"


def tool_spans(collector):
    return [span for span in collector.spans() if span["attributes"].get("mcp.method.name") == "tools/call"]


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize("collector", ["http/protobuf", "grpc"], indirect=True)
async def test_remote_parent_and_terminal_log_are_preserved(tmp_path, collector, transport):
    async with WireServer(tmp_path, collector, transport=transport) as server:
        carrier = {"traceparent": PARENT, "tracestate": "vendor=fixture"}
        response = await server.call(
            meta=carrier if transport == "stdio" else None,
            headers=carrier if transport == "http" else None,
        )
        assert response["result"]["structuredContent"]["status"] == "ok"
    spans = tool_spans(collector)
    assert len(spans) == 1
    span = spans[0]
    assert span["kind"] == 2
    assert span["trace_id"] == "11" * 16
    assert span["parent_span_id"] == "22" * 8
    assert span["span_id"] != "22" * 8
    assert span["tracestate"] == "vendor=fixture"
    assert span["resource"]["justpen.session.id"] == "pentest-a"
    assert span["resource"]["service.name"] == "justpen-knowledgebase-mcp"
    assert span["attributes"]["gen_ai.tool.name"] == "kb_status"
    assert span["attributes"]["justpen.trace.context.source"] == ("meta" if transport == "stdio" else "http")
    started = [
        item
        for item in collector.logs()
        if item["body"] == "mcp.request.started"
        and (item["trace_id"], item["span_id"]) == (span["trace_id"], span["span_id"])
    ]
    assert len(started) == 1
    assert started[0]["attributes"]["gen_ai.tool.name"] == "kb_status"
    terminal = [
        item
        for item in collector.logs()
        if item["body"] == "mcp.request.finished"
        and (item["trace_id"], item["span_id"]) == (span["trace_id"], span["span_id"])
    ]
    assert len(terminal) == 1
    assert terminal[0]["resource"]["service.name"] == "justpen-knowledgebase-mcp"
    assert collector.signals() == {"traces", "logs", "metrics"}


@pytest.mark.parametrize("stateless", [False, True])
async def test_http_header_precedence_invalid_fallback_and_new_roots(tmp_path, collector, stateless):
    cases = [
        ({"traceparent": PARENT}, {"traceparent": OTHER}, "http", False, True, "11" * 16, "22" * 8),
        ({"traceparent": "invalid"}, {"traceparent": OTHER}, "meta", True, False, "33" * 16, "44" * 8),
        (None, {}, "new_root", False, False, None, ""),
        ({"traceparent": "invalid"}, {"traceparent": []}, "new_root", True, False, None, ""),
    ]
    async with WireServer(tmp_path, collector, transport="http", stateless=stateless) as server:
        for index, (headers, meta, *_expected) in enumerate(cases):
            await server.call(meta={**meta, "callId": f"case-{index}"}, headers=headers)
    spans = tool_spans(collector)
    assert len(spans) == len(cases)
    by_call = {span["attributes"]["gen_ai.tool.call.id"]: span for span in spans}
    roots = []
    for index, (_headers, _meta, source, invalid, conflict, trace_id, parent_id) in enumerate(cases):
        span = by_call[f"case-{index}"]
        attrs = span["attributes"]
        assert attrs["justpen.trace.context.source"] == source
        assert attrs["justpen.trace.context.invalid"] is invalid
        assert attrs["justpen.trace.context.conflict"] is conflict
        assert span["parent_span_id"] == parent_id
        if trace_id is None:
            roots.append(span["trace_id"])
        else:
            assert span["trace_id"] == trace_id
    assert len(set(roots)) == len(roots)


@pytest.mark.parametrize(("transport", "stateless"), [("stdio", False), ("http", False), ("http", True)])
async def test_concurrent_calls_keep_trace_and_result_isolation(tmp_path, collector, transport, stateless):
    async with WireServer(tmp_path, collector, transport=transport, stateless=stateless, fixture_server=True) as server:
        first, second = await asyncio.gather(
            server.call(
                arguments={"secret": "a"},
                meta={"traceparent": PARENT, "callId": "call-a"},
                request_id=90 if stateless else None,
            ),
            server.call(
                arguments={"secret": "b"},
                meta={"traceparent": OTHER, "callId": "call-b"},
                request_id=91 if stateless else None,
            ),
        )
        assert first["result"]["structuredContent"]["status"] == "ok"
        assert second["result"]["structuredContent"]["status"] == "ok"
        assert sorted(first["result"]["structuredContent"]["data"]["arrived"]) == ["a", "b"]
    spans = tool_spans(collector)
    assert len(spans) == 2
    by_call = {span["attributes"]["gen_ai.tool.call.id"]: span for span in spans}
    assert by_call["call-a"]["trace_id"] == "11" * 16
    assert by_call["call-a"]["attributes"]["justpen.result.status"] == "success"
    assert by_call["call-b"]["trace_id"] == "33" * 16
    assert by_call["call-b"]["attributes"]["justpen.result.status"] == "success"


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_unsampled_parent_still_correlates_terminal_log(tmp_path, collector, transport):
    async with WireServer(tmp_path, collector, transport=transport) as server:
        await server.call(meta={"traceparent": PARENT[:-2] + "00", "callId": "unsampled"})
    assert not tool_spans(collector)
    terminal = [
        item
        for item in collector.logs()
        if item["body"] == "mcp.request.finished" and item["attributes"].get("gen_ai.tool.call.id") == "unsampled"
    ]
    assert len(terminal) == 1
    assert terminal[0]["trace_id"] == "11" * 16
    assert terminal[0]["span_id"] not in {"", "0" * 16, "22" * 8}


async def test_native_client_identifiers_remain_separate_from_resource_session(tmp_path, collector):
    meta = {
        "callId": "call_original",
        "threadId": "thread-a",
        "itemId": "item-a",
        "x-codex-turn-metadata": {
            "session_id": "native-session",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
        },
    }
    async with WireServer(tmp_path, collector, client_name="codex-mcp-client") as server:
        await server.call(meta=meta)
    attrs = tool_spans(collector)[0]["attributes"]
    assert attrs["gen_ai.tool.call.id"] == "call_original"
    assert attrs["justpen.client.session.id"] == "native-session"
    assert tool_spans(collector)[0]["resource"]["justpen.session.id"] == "pentest-a"


@pytest.mark.parametrize(
    ("meta", "expected_call", "conflict"),
    [
        ({"claudecode/toolUseId": "toolu_original"}, "toolu_original", False),
        ({"callId": "codex", "claudecode/toolUseId": "claude"}, None, True),
    ],
)
async def test_claude_call_id_and_cross_client_conflict_are_bounded(tmp_path, collector, meta, expected_call, conflict):
    meta = {**meta, "traceparent": PARENT}
    async with WireServer(tmp_path, collector, client_name="claude-code") as server:
        await server.call(meta=meta)
    attrs = tool_spans(collector)[0]["attributes"]
    assert attrs.get("gen_ai.tool.call.id") == expected_call
    assert attrs.get("justpen.correlation.conflict", False) is conflict


@pytest.mark.parametrize("collector", ["http/protobuf", "grpc"], indirect=True)
async def test_enabled_runtime_clears_inherited_otel_and_uses_scoped_export_settings(tmp_path, collector):
    env = {
        "OTEL_SDK_DISABLED": "true",
        "OTEL_PYTHON_TRACER_PROVIDER": "sentinel-unavailable-provider",
        "OTEL_PROPAGATORS": "sentinel-unavailable-propagator",
        "OTEL_RESOURCE_ATTRIBUTES": "justpen.session.id=wrong,leaked=sentinel-secret",
        "OTEL_SERVICE_NAME": "wrong-service",
        "OTEL_TRACES_SAMPLER": "always_off",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:1",
        "OTEL_EXPORTER_OTLP_HEADERS": "authorization=ambient-secret",
        PREFIX + "HEADERS": "authorization=utility-secret",
        PREFIX + "TRACES_HEADERS": "authorization=trace-secret",
        PREFIX + "TRACES_ENDPOINT": collector.endpoint
        + ("/custom/traces" if collector.protocol == "http/protobuf" else ""),
        PREFIX + "RESOURCE_ATTRIBUTES": "deployment.environment.name=test,justpen.session.id=wrong",
    }
    async with WireServer(tmp_path, collector, env=env) as server:
        await server.call(meta={"traceparent": PARENT})
    for span in collector.spans():
        assert span["resource"]["service.name"] == "justpen-knowledgebase-mcp"
        assert span["resource"]["justpen.session.id"] == "pentest-a"
        assert span["resource"]["deployment.environment.name"] == "test"
        assert "leaked" not in span["resource"]
    for name, data, headers in collector.records:
        received = {key.lower(): value for key, value in headers.items()}
        assert received["authorization"] == ("trace-secret" if name == "traces" else "utility-secret")
        assert b"secret" not in data
    if collector.protocol == "http/protobuf":
        assert set(collector.paths) == {"/custom/traces", "/v1/logs", "/v1/metrics"}


async def test_oversized_future_metadata_opens_a_root(tmp_path, collector):
    async with WireServer(tmp_path, collector) as server:
        await server.call(meta={"traceparent": "01" + PARENT[2:] + "-" + "x" * 600})
    span = tool_spans(collector)[0]
    assert span["parent_span_id"] == ""
    assert span["attributes"]["justpen.trace.context.source"] == "new_root"
    assert span["attributes"]["justpen.trace.context.invalid"] is True


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_kb_payload_and_native_structural_errors_never_export_content(tmp_path, collector, transport):
    private = "sentinel-secret"
    async with WireServer(tmp_path, collector, transport=transport) as server:
        written = await server.call(
            "kb_write",
            {"nodes": [{"type": "domain", "properties": {"value": "private.example", "credential": private}}]},
            meta={"traceparent": PARENT},
        )
        assert written["result"]["structuredContent"]["status"] == "ok"
        # Structural type validation fails inside native FastMCP, before invoke's envelope.
        structural = await server.call("kb_write", {"nodes": private}, meta={"traceparent": PARENT})
        assert structural["result"]["isError"]
        unknown = await server.call(private, {"query": private})
        assert "error" in unknown or unknown["result"]["isError"]
        denied = await server.call("kb_ingest_evidence", {"path": "/private/" + private})
        assert denied["result"]["isError"]
        ingested = await server.call(
            "kb_ingest_evidence",
            {"text": "evidence-" + private, "source": "source-" + private},
            meta={"traceparent": PARENT, "callId": "ingest-call"},
        )
        assert ingested["result"]["structuredContent"]["status"] == "ok"
        await server.call("kb_search", {"kind": "nodes", "query": private})
    raw = b"".join(data for _name, data, _headers in collector.records)
    assert private.encode() not in raw
    assert private not in server.stderr_path.read_text()
    assert b"private.example" not in raw
    assert str(tmp_path).encode() not in raw
    initiating = next(
        span for span in tool_spans(collector) if span["attributes"].get("gen_ai.tool.call.id") == "ingest-call"
    )
    jobs = [span for span in collector.spans() if span["attributes"].get("justpen.operation.kind") == "ingest"]
    assert jobs
    assert all(span["links"] == [(initiating["trace_id"], initiating["span_id"])] for span in jobs)
    assert all(span["parent_span_id"] == "" for span in jobs)
    errors = [span for span in tool_spans(collector) if span["attributes"].get("justpen.result.status") == "error"]
    assert len(errors) >= 3
    assert all(span["status"] == 2 for span in errors)
    for payload in collector.metrics():
        for resource in payload["resource_metrics"]:
            for scope in resource["scope_metrics"]:
                for metric in scope["metrics"]:
                    for point in metric.get("sum", metric.get("histogram", {}))["data_points"]:
                        keys = {item["key"] for item in point.get("attributes", [])}
                        assert keys <= {
                            "mcp.method.name",
                            "gen_ai.tool.name",
                            "justpen.transport",
                            "justpen.result.status",
                            "justpen.operation.kind",
                        }


async def test_different_native_http_sessions_share_canonical_identity(tmp_path, collector):
    async with WireServer(tmp_path, collector, transport="http") as server:
        assert server.http is not None
        sessions = []
        for native in ("native-a", "native-b"):
            if sessions:
                server.http.headers.pop("Mcp-Session-Id")
                await server.request(
                    "initialize",
                    {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": native, "version": "1"},
                    },
                )
                await server.notify("notifications/initialized", {})
            sessions.append(server.http.headers["Mcp-Session-Id"])
            await server.call(
                meta={
                    "callId": native,
                    "justpen.session.id": "forged",
                    "x-codex-turn-metadata": {"session_id": native},
                },
                headers={"justpen.session.id": "forged", "JUSTPEN_SESSION_ID": "forged"},
            )
        assert sessions[0] != sessions[1]
    spans = tool_spans(collector)
    assert len(spans) == 2
    assert {span["resource"]["justpen.session.id"] for span in spans} == {"pentest-a"}
    assert {span["attributes"]["justpen.client.session.id"] for span in spans} == {"native-a", "native-b"}


async def test_reindex_restart_links_original_native_call_and_reuse_preserves_context(tmp_path, collector):

    async with WireServer(tmp_path, collector, fixture_server=True, env={"KB_TELEMETRY_HOLD_JOBS": "1"}) as server:
        first = await server.call(
            "kb_reindex",
            {"kind": "nodes", "all": True},
            meta={"traceparent": PARENT, "callId": "original", "baggage": "secret=sentinel-secret"},
        )
        second = await server.call(
            "kb_reindex", {"kind": "nodes", "all": True}, meta={"traceparent": OTHER, "callId": "reuser"}
        )
        original = first["result"]["structuredContent"]["data"]
        reused = second["result"]["structuredContent"]["data"]
        assert reused["reused"] is True
        assert reused["job_id"] == original["job_id"]
    initiator = next(
        span for span in tool_spans(collector) if span["attributes"].get("gen_ai.tool.call.id") == "original"
    )
    with apsw.Connection(str(tmp_path / ".justpen/knowledgebase/graph.sqlite3")) as connection:
        payload = json.loads(connection.execute("SELECT payload FROM jobs WHERE uuid=?", (original["job_id"],)).get)
        assert connection.execute("SELECT query_epoch FROM settings").get == 2
        assert set(payload["_telemetry"]) <= {"traceparent", "tracestate"}
        assert (
            payload["_telemetry"]["traceparent"] == "00-" + initiator["trace_id"] + "-" + initiator["span_id"] + "-01"
        )
    async with WireServer(tmp_path, collector) as server:
        response = {}
        for _ in range(100):
            response = await server.call("kb_jobs", {"action": "get", "job_id": original["job_id"]})
            if response["result"]["structuredContent"]["data"]["state"] == "completed":
                break
            await asyncio.sleep(0.01)
        assert response["result"]["structuredContent"]["data"]["state"] == "completed"
        assert "_telemetry" not in json.dumps(response)
    steps = [span for span in collector.spans() if span["attributes"].get("justpen.operation.kind") == "reindex"]
    assert steps
    assert all(span["parent_span_id"] == "" for span in steps)
    assert all(span["trace_id"] != initiator["trace_id"] for span in steps)
    assert all(span["links"] == [(initiator["trace_id"], initiator["span_id"])] for span in steps)
    assert b"sentinel-secret" not in b"".join(data for _, data, _ in collector.records)


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize("enabled", ["true", "false"])
async def test_stderr_native_validation_and_exception_redaction_with_or_without_export(
    tmp_path, collector, transport, enabled
):
    async with WireServer(
        tmp_path, collector, transport=transport, fixture_server=True, env={PREFIX + "ENABLED": enabled}
    ) as server:
        invalid = await server.call(arguments={"secret": ["sentinel-secret"]})
        failed = await server.call(arguments={"secret": "fail-value"})
        assert invalid["result"]["isError"]
        assert failed["result"]["isError"]
    stderr = server.stderr_path.read_text()
    assert "sentinel-secret" not in stderr
    assert "fastmcp.server.server" in stderr
    assert "WARNING" in stderr
    assert "ERROR" in stderr
    assert b"sentinel-secret" not in b"".join(data for _, data, _ in collector.records)
    if enabled == "false":
        assert not collector.records


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize(
    ("name", "meta", "call_id"),
    [
        (
            "codex-mcp-client",
            {
                "callId": "codex-call",
                "threadId": "thread-a",
                "itemId": "item-a",
                "x-codex-turn-metadata": {"session_id": "native-session", "thread_id": "thread-a", "turn_id": "turn-a"},
            },
            "codex-call",
        ),
        ("claude-code", {"claudecode/toolUseId": "toolu_original"}, "toolu_original"),
    ],
)
async def test_browser_reference_harness_metadata_without_w3c_never_fabricates_parent(
    tmp_path, collector, transport, name, meta, call_id
):
    async with WireServer(tmp_path, collector, transport=transport, client_name=name) as server:
        await server.call(meta=meta)
    span = tool_spans(collector)[0]
    assert span["parent_span_id"] == ""
    assert span["attributes"]["justpen.trace.context.source"] == "new_root"
    assert span["attributes"]["gen_ai.tool.call.id"] == call_id
    assert span["attributes"]["justpen.client.name"] == name
    assert span["resource"]["justpen.session.id"] == "pentest-a"
    assert span["attributes"].get("justpen.client.session.id") == (
        "native-session" if name == "codex-mcp-client" else None
    )
