"""Loopback OTLP receivers and raw JSON-RPC clients for real transport assertions."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import grpc
import httpx2
from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest, ExportLogsServiceResponse
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
    ExportMetricsServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from typing_extensions import override

PREFIX = "JUSTPEN_KNOWLEDGEBASE_OTEL_"
MESSAGES = {
    "traces": (ExportTraceServiceRequest, ExportTraceServiceResponse, "trace.v1.TraceService"),
    "logs": (ExportLogsServiceRequest, ExportLogsServiceResponse, "logs.v1.LogsService"),
    "metrics": (ExportMetricsServiceRequest, ExportMetricsServiceResponse, "metrics.v1.MetricsService"),
}


def attributes(values):
    return {item.key: getattr(item.value, item.value.WhichOneof("value")) for item in values}


def serialize(message: Any) -> bytes:
    return message.SerializeToString()


class Collector:
    def __init__(self, protocol="http/protobuf", *, status=200):
        self.protocol = protocol
        self.records = []
        self.paths = []
        self.status = status
        self.executor = None
        self.thread = None
        if protocol == "grpc":
            self.executor = ThreadPoolExecutor(max_workers=3)
            self.server = grpc.server(self.executor)
            for name, (request, response, service) in MESSAGES.items():

                def capture(message, context, name=name, response=response):
                    self.records.append((name, message.SerializeToString(), dict(context.invocation_metadata())))
                    return response()

                handler = grpc.unary_unary_rpc_method_handler(
                    capture,
                    request_deserializer=request.FromString,
                    response_serializer=serialize,
                )
                self.server.add_generic_rpc_handlers(
                    (
                        grpc.method_handlers_generic_handler(
                            "opentelemetry.proto.collector." + service,
                            {"Export": handler},
                        ),
                    )
                )
            port = self.server.add_insecure_port("127.0.0.1:0")
            self.server.start()
        else:
            owner = self

            class Handler(BaseHTTPRequestHandler):
                def do_POST(self):
                    data = self.rfile.read(int(self.headers["Content-Length"]))
                    name = self.path.rstrip("/").rsplit("/", 1)[-1]
                    owner.paths.append(self.path)
                    owner.records.append((name, data, dict(self.headers.items())))
                    body = b"" if owner.status == 200 else b"collector-sentinel-secret"
                    self.send_response(owner.status)
                    self.send_header("Content-Type", "application/x-protobuf")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                @override
                def log_message(self, format: str, *args: Any) -> None:
                    pass

            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self.server.daemon_threads = True
            port = self.server.server_port
            self.thread = threading.Thread(
                target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
            )
            self.thread.start()
        self.endpoint = f"http://127.0.0.1:{port}"

    def close(self):
        if isinstance(self.server, ThreadingHTTPServer):
            self.server.shutdown()
            self.server.server_close()
            assert self.thread is not None
            self.thread.join(5)
        else:
            self.server.stop(0).wait(5)
            assert self.executor is not None
            self.executor.shutdown()

    def signals(self):
        return {name for name, _data, _headers in self.records}

    def spans(self):
        result = []
        for name, data, _headers in self.records:
            if name != "traces":
                continue
            for resource in ExportTraceServiceRequest.FromString(data).resource_spans:
                for scope in resource.scope_spans:
                    result.extend(
                        {
                            "name": span.name,
                            "trace_id": span.trace_id.hex(),
                            "span_id": span.span_id.hex(),
                            "parent_span_id": span.parent_span_id.hex(),
                            "tracestate": span.trace_state,
                            "links": [(link.trace_id.hex(), link.span_id.hex()) for link in span.links],
                            "kind": span.kind,
                            "status": span.status.code,
                            "attributes": attributes(span.attributes),
                            "resource": attributes(resource.resource.attributes),
                        }
                        for span in scope.spans
                    )
        return result

    def logs(self):
        result = []
        for name, data, _headers in self.records:
            if name != "logs":
                continue
            for resource in ExportLogsServiceRequest.FromString(data).resource_logs:
                for scope in resource.scope_logs:
                    result.extend(
                        {
                            "body": record.body.string_value,
                            "trace_id": record.trace_id.hex(),
                            "span_id": record.span_id.hex(),
                            "attributes": attributes(record.attributes),
                            "resource": attributes(resource.resource.attributes),
                        }
                        for record in scope.log_records
                    )
        return result

    def metrics(self):
        return [
            MessageToDict(ExportMetricsServiceRequest.FromString(data), preserving_proto_field_name=True)
            for name, data, _headers in self.records
            if name == "metrics"
        ]


class WireServer:
    def __init__(
        self,
        tmp_path,
        collector,
        *,
        transport="stdio",
        stateless=False,
        env=None,
        client_name="fixture-client",
        fixture_server=False,
    ):
        self.transport = transport
        self.client_name = client_name
        self.fixture_server = fixture_server
        self.pending = {}
        self.next_id = 1
        self.stderr_path = tmp_path / "server.stderr"
        self.env = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith(("OTEL_", PREFIX, "FASTMCP_", "UTILITY_MCP_")) and name != "JUSTPEN_SESSION_ID"
        }
        self.env.update(
            {
                PREFIX + "ENABLED": "true",
                PREFIX + "ENDPOINT": collector.endpoint,
                PREFIX + "PROTOCOL": collector.protocol,
                PREFIX + "BSP_SCHEDULE_DELAY": "20",
                PREFIX + "BLRP_SCHEDULE_DELAY": "20",
                PREFIX + "METRIC_EXPORT_INTERVAL": "50",
                PREFIX + "TIMEOUT": "0.2",
                PREFIX + "SHUTDOWN_TIMEOUT_MS": "500",
                "JUSTPEN_SESSION_ID": "pentest-a",
                "JUSTPEN_KNOWLEDGEBASE_WORKSPACE_DIR": str(tmp_path),
                "FASTMCP_JSON_RESPONSE": "true",
                "FASTMCP_STATELESS_HTTP": str(stateless).lower(),
            }
        )
        self.env.update(env or {})
        self.http = None
        self.reader = None
        self.process = None
        self.stderr_file = None

    async def __aenter__(self):
        command = (
            [sys.executable, str(Path(__file__).with_name("server.py"))]
            if self.fixture_server
            else [sys.executable, "-B", "-m", "justpen_knowledgebase_mcp"]
        )
        if self.transport == "http":
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            command.extend(["--transport", "http", "--host", "127.0.0.1", "--port", str(port)])
            self.http = httpx2.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                timeout=10,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-11-25",
                },
            )
        self.stderr_file = self.stderr_path.open("wb")
        self.process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self.stderr_file,
            env=self.env,
            limit=2 * 1024 * 1024,
        )
        try:
            if self.http:
                await self._wait_for_http()
            else:
                self.reader = asyncio.create_task(self._read_stdio())
            result = await self.request(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": self.client_name, "version": "1"},
                },
            )
            assert "result" in result, result
            await self.notify("notifications/initialized", {})
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        else:
            return self

    async def _wait_for_http(self):
        assert self.http is not None
        assert self.process is not None
        deadline = time.monotonic() + 15
        while True:
            if self.process.returncode is not None:
                raise AssertionError(self.stderr_path.read_text()[-10000:])
            try:
                await self.http.get("/", timeout=0.2)
                break
            except httpx2.TransportError:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.02)

    async def _read_stdio(self):
        assert self.process is not None
        assert self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                request_id = message.get("id")
                future = self.pending.pop(request_id, None)
                if future is not None and not future.done():
                    future.set_result(message)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("MCP process closed before response"))

    async def request(self, method, params, *, meta=None, headers=None, request_id=None):
        if request_id is None:
            request_id = self.next_id
            self.next_id += 1
        params = dict(params)
        if meta is not None:
            params["_meta"] = meta
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        if self.http:
            response = await self.http.post("/mcp", json=message, headers=headers)
            response.raise_for_status()
            if session := response.headers.get("mcp-session-id"):
                self.http.headers["Mcp-Session-Id"] = session
            return response.json()
        assert not headers
        assert self.process is not None
        assert self.process.stdin is not None
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        await self.process.stdin.drain()
        return await asyncio.wait_for(future, 10)

    async def notify(self, method, params):
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if self.http:
            response = await self.http.post("/mcp", json=message)
            response.raise_for_status()
        else:
            assert self.process is not None
            assert self.process.stdin is not None
            self.process.stdin.write(json.dumps(message).encode() + b"\n")
            await self.process.stdin.drain()

    async def call(self, tool="kb_status", arguments=None, **kwargs):
        if arguments is None:
            arguments = {}
        return await self.request("tools/call", {"name": tool, "arguments": arguments}, **kwargs)

    async def __aexit__(self, *args):
        if self.process is not None:
            if self.process.returncode is None:
                self.process.send_signal(signal.SIGTERM)
            if self.process.stdin is not None:
                # The SDK's blocking read thread needs the client's EOF during teardown.
                self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
                raise
            finally:
                try:
                    if self.reader is not None:
                        await self.reader
                finally:
                    if self.http is not None:
                        await self.http.aclose()
                    if self.stderr_file is not None:
                        self.stderr_file.close()
            assert self.process.returncode == 0, self.stderr_path.read_text()[-10000:]
