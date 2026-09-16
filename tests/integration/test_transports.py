"""Actual stdio/HTTP process contracts, cancellation and shared workspace."""

import asyncio
import base64
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from uuid import uuid4

import httpx2
import pytest
from fastmcp import Client

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.models import DeleteRequest
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.admission import open_lock
from justpen_knowledgebase_mcp.storage.jobs import JobStore
from justpen_knowledgebase_mcp.storage.maintenance import WalState

from ..tools import envelope
from .mcp_client import (
    CANCELLATION_PROBE,
    client_for,
    environment,
    free_port,
    http_server,
    initialize,
    process,
    wait_file,
    wire_client,
    wire_request,
)

pytestmark = pytest.mark.integration

# Explicit wildcard binding and rejection are required transport acceptance cases.
WILDCARD_HOST = "0.0.0.0"  # noqa: S104


def test_stdio_devnull_exits_cleanly(tmp_path):
    child = subprocess.run(
        [sys.executable, "-B", "-m", "justpen_knowledgebase_mcp"],
        env=environment(tmp_path),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout == ""
    assert "Traceback" not in child.stderr


@pytest.mark.parametrize("ending", ["idle-signal", "partial-signal", "eof"])
async def test_stdio_normal_shutdown_with_open_or_partial_stdin(tmp_path, ending):
    async with process(tmp_path) as child:
        await initialize(child)
        assert child.stdin is not None
        if ending == "partial-signal":
            child.stdin.write(b'{"jsonrpc":"2.0","id":')
            await child.stdin.drain()
        if ending == "eof":
            child.stdin.close()
        else:
            child.send_signal(signal.SIGTERM)
        await asyncio.wait_for(child.wait(), 3)
        assert child.stderr is not None
        stderr = await child.stderr.read()
        assert child.returncode == 0, stderr.decode(errors="replace")
        assert b"shutdown_timeout" not in stderr


async def test_stdio_signal_with_pending_sdk_relay_send(tmp_path):
    probe = """
import asyncio, os
from pathlib import Path
import anyio
from anyio.streams.memory import MemoryObjectSendStream
from mcp.shared._context_streams import ContextReceiveStream
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from justpen_knowledgebase_mcp.__main__ import cli
root = Path(os.environ["JUSTPEN_KNOWLEDGEBASE_WORKSPACE_DIR"])
replayed = None
dispatch = JSONRPCDispatcher._dispatch
send = MemoryObjectSendStream.send
close = ContextReceiveStream.aclose
async def hold_after_initialize(self, item, *args):
    global replayed
    await dispatch(self, item, *args)
    if getattr(getattr(item, "message", None), "method", None) == "initialize":
        replayed = self._read_stream
        await anyio.sleep_forever()
async def observe_relay(self, item):
    if replayed is not None and self._state is replayed._inner._state:
        def pending():
            assert self.statistics().tasks_waiting_send == 1
            (root / "relay-pending").write_text("pending")
        loop = asyncio.get_running_loop()
        # First run the send's checkpoint, then observe the actual blocked send.
        loop.call_soon(loop.call_soon, pending)
    await send(self, item)
async def close_then_yield(self):
    await close(self)
    if self is replayed:
        # Widen the real dispatcher-close / relay-task-group-exit interval.
        await anyio.lowlevel.cancel_shielded_checkpoint()
JSONRPCDispatcher._dispatch = hold_after_initialize
MemoryObjectSendStream.send = observe_relay
ContextReceiveStream.aclose = close_then_yield
cli()
"""
    async with process(tmp_path, probe=probe) as child:
        await initialize(child)
        await wait_file(tmp_path / "relay-pending")
        assert child.stdin is not None
        child.stdin.write(b'{"jsonrpc":"2.0","id":')
        await child.stdin.drain()
        child.send_signal(signal.SIGTERM)
        await asyncio.wait_for(child.wait(), 3)
        assert child.stderr is not None
        stderr = await child.stderr.read()
        assert child.returncode == 0, stderr.decode(errors="replace")
        assert b"shutdown_timeout" not in stderr


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_all_tools_and_presence_over_real_wire(tmp_path, transport):

    async with client_for(tmp_path, transport) as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools} == {
            "kb_status",
            "kb_types",
            "kb_write",
            "kb_get",
            "kb_search",
            "kb_neighbors",
            "kb_delete",
            "kb_ingest_evidence",
            "kb_read_evidence",
            "kb_jobs",
            "kb_reindex",
        }
        status = envelope(await client.call_tool("kb_status", {}))["data"]
        assert status["bind_scope"] == ("stdio" if transport == "stdio" else "loopback")
        assert status["authentication"] == "none"
        assert status["deployment_scope"] == "single_workspace"
        assert status["engagement_scope"] == "single_engagement"
        assert status["request_workspace_selection"] is False
        assert status["canonical_session_source"] == "JUSTPEN_SESSION_ID"
        await client.call_tool("kb_types", {"kind": "nodes", "type": "hostname"})
        props = {"name": "exact.example", "array": [True, 1, 1.25, None, "Ä\n"], "nested": {"null": None}}
        result = envelope(
            await client.call_tool(
                "kb_write", {"nodes": [{"type": "hostname", "properties": props, "label": "keep", "source": "keep"}]}
            )
        )["data"]
        identifier = result["nodes"][0]["id"]
        await client.call_tool("kb_write", {"nodes": [{"id": identifier}]})
        record = envelope(await client.call_tool("kb_get", {"kind": "nodes", "ids": [identifier]}))["data"]["records"][
            0
        ]
        assert record["label"] == "keep"
        assert record["source"] == "keep"
        assert record["properties"] == props
        await client.call_tool("kb_write", {"nodes": [{"id": identifier, "label": None, "source": None}]})
        record = envelope(await client.call_tool("kb_get", {"kind": "nodes", "ids": [identifier]}))["data"]["records"][
            0
        ]
        assert record["label"] is None
        assert record["source"] is None
        assert record["properties"] == props
        await client.call_tool("kb_search", {"kind": "evidence"})
        for explicit in (True, False):
            invalid = await client.call_tool(
                "kb_search", {"kind": "evidence", "include_evidence": explicit}, raise_on_error=False
            )
            assert envelope(invalid)["error"].startswith("INVALID:")
        neighbors = envelope(await client.call_tool("kb_neighbors", {"seed_ids": [identifier]}))["data"]
        assert neighbors["nodes"][0]["id"] == identifier
        job = envelope(
            await client.call_tool(
                "kb_ingest_evidence", {"text": "exact\r\nproof Ä", "targets": [{"kind": "nodes", "id": identifier}]}
            )
        )["data"]
        evidence_id = job["evidence_id"]
        assert (
            envelope(await client.call_tool("kb_read_evidence", {"evidence_id": evidence_id}))["data"]["content"]
            == "exact\r\nproof Ä"
        )
        assert (
            envelope(await client.call_tool("kb_jobs", {"action": "get", "job_id": job["job_id"]}))["data"]["job_id"]
            == job["job_id"]
        )
        await client.call_tool("kb_reindex", {"kind": "evidence", "ids": [evidence_id]})
        await client.call_tool("kb_delete", {"kind": "nodes", "ids": [identifier], "cascade": True})


@pytest.mark.parametrize("host", [WILDCARD_HOST, "::", "example.test"])
async def test_nonloopback_denied_before_socket(tmp_path, host):

    port = free_port()
    async with process(tmp_path, "--transport", "http", "--host", host, "--port", str(port)) as child:
        out, err = await asyncio.wait_for(child.communicate(), 5)
        assert child.returncode != 0
        assert b"CONFIGURATION" in err
        assert out == b""
        with socket.socket() as sock:
            assert sock.connect_ex(("127.0.0.1", port)) != 0
        assert not (tmp_path / ".justpen").exists()


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", WILDCARD_HOST])
async def test_http_origin_guard_survives_bind_flag_and_cli_override(tmp_path, host):

    async with http_server(
        tmp_path, host=host, allow=host == WILDCARD_HOST, env={"JUSTPEN_KNOWLEDGEBASE_HOST": "denied.example"}
    ) as (url, _child):
        async with httpx2.AsyncClient() as http:
            result = await http.post(
                url,
                headers={"Origin": "https://invalid.example"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            )
            assert result.status_code == 403
            result = await http.get(url, headers={"Host": "invalid.example"})
            assert result.status_code == 421
        async with Client(url) as client:
            status = envelope(await client.call_tool("kb_status", {}))["data"]
            assert status["bind_scope"] == ("non_loopback" if host == WILDCARD_HOST else "loopback")


async def test_http_explicit_host_alias_keeps_strict_host_and_origin_guard(tmp_path):
    async with (
        http_server(
            tmp_path,
            host=WILDCARD_HOST,
            allow=True,
            env={"JUSTPEN_KNOWLEDGEBASE_ALLOWED_HOSTS": '["kb.example.test"]'},
        ) as (url, _child),
        httpx2.AsyncClient() as http,
    ):
        allowed = await http.get(url, headers={"Host": "kb.example.test"})
        assert allowed.status_code != 421
        unknown = await http.get(url, headers={"Host": "unknown.example.test"})
        assert unknown.status_code == 421
        bad_origin = await http.get(url, headers={"Host": "kb.example.test", "Origin": "https://invalid.example.test"})
        assert bad_origin.status_code == 403


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize("phase", ["precommit", "committing"])
async def test_real_cancellation_notification_and_late_cancel(tmp_path, transport, phase):

    (tmp_path / "mode").write_text(phase)
    async with wire_client(tmp_path, transport, probe=CANCELLATION_PROBE) as client:
        request = asyncio.create_task(
            client.request(
                "cancel-1",
                "tools/call",
                {
                    "name": "kb_write",
                    "arguments": {"nodes": [{"type": "hostname", "properties": {"name": "cancel.example"}}]},
                },
            )
        )
        await wait_file(tmp_path / "entered")
        assert (tmp_path / "entered").read_text() == ("running" if phase == "precommit" else "committing")
        await client.notify("notifications/cancelled", {"requestId": "cancel-1", "reason": "test cancellation"})
        await asyncio.sleep(0.1)
        (tmp_path / "release").touch()
        # Cancelled transports need not deliver a response. Drain or abandon the
        # client wait independently; durable DB state is the authority below.
        await asyncio.wait({request}, timeout=1)
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        response = await client.request(4, "tools/call", {"name": "kb_search", "arguments": {"kind": "nodes"}})
        items = response["result"]["structuredContent"]["data"]["items"]
        assert len(items) == (0 if phase == "precommit" else 1)
        await client.notify("notifications/cancelled", {"requestId": "cancel-1"})
        response = await client.request(
            5,
            "tools/call",
            {
                "name": "kb_write",
                "arguments": {"nodes": [{"type": "hostname", "properties": {"name": "next.example"}}]},
            },
        )
        assert response["result"]["structuredContent"]["status"] == "ok"


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_raw_duplicate_keys_last_wins_and_nonfinite_rejected(tmp_path, transport):

    async with wire_client(tmp_path, transport) as client:
        result = await client.raw(
            2,
            b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"kb_write","arguments":{"nodes":[{"type":"hostname","properties":{"name":"first.example","name":"last.example"}}]}}}',
        )
        identifier = result["result"]["structuredContent"]["data"]["nodes"][0]["id"]
        result = await client.request(
            3, "tools/call", {"name": "kb_get", "arguments": {"kind": "nodes", "ids": [identifier]}}
        )
        assert result["result"]["structuredContent"]["data"]["records"][0]["properties"]["name"] == "last.example"
        for identifier, invalid in enumerate(("NaN", "Infinity", "-Infinity"), 4):
            body = (
                '{"jsonrpc":"2.0","id":$TOKEN,"method":"tools/call","params":{"name":"kb_write","arguments":{"nodes":[{"type":"hostname","properties":{"name":"bad.example","nested":{"value":$VALUE}}}]}}}'.replace(
                    "$TOKEN", str(identifier)
                ).replace("$VALUE", invalid)
            ).encode()
            result = await client.raw(identifier, body)
            assert result["result"]["isError"]
            # SDK argument validation precedes the application envelope.
            assert "number must be finite" in result["result"]["content"][0]["text"]
        result = await client.request(9, "tools/call", {"name": "kb_search", "arguments": {"kind": "nodes"}})
        assert len(result["result"]["structuredContent"]["data"]["items"]) == 1


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize("kind", ["nodes", "relations", "evidence"])
async def test_delete_atomic_pending_cascade_and_retry_contract(tmp_path, transport, kind):

    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        graph = await kb.write(
            {
                "nodes": [
                    {"type": "domain", "properties": {"name": name}} for name in ("a.example", "b.example", "example")
                ],
                "relations": [
                    {
                        "type": "subdomain_of",
                        "properties": {},
                        "source_ref": {"node_index": index},
                        "target_ref": {"node_index": 2},
                    }
                    for index in (0, 1)
                ],
            }
        )
        nodes = [node["id"] for node in graph["nodes"]]
        relations = [relation["id"] for relation in graph["relations"]]
        evidence = []
        for index in (0, 1):
            job = await kb.ingest_evidence(
                {
                    "text": f"linked-{index}",
                    "targets": [{"kind": "nodes", "id": nodes[index]}, {"kind": "relations", "id": relations[index]}],
                }
            )
            evidence.append(job["evidence_id"])
        ids = {"nodes": nodes[:2], "relations": relations, "evidence": evidence}[kind]
        async with client_for(tmp_path, transport) as client:
            duplicate = envelope(
                await client.call_tool("kb_delete", {"kind": kind, "ids": [ids[0], ids[0]]}, raise_on_error=False)
            )
            assert duplicate["error"].startswith("INVALID:")
            missing = "e_" + "0" * 64 if kind == "evidence" else str(uuid4())
            absent = envelope(
                await client.call_tool(
                    "kb_delete", {"kind": kind, "ids": [ids[0], missing], "cascade": True}, raise_on_error=False
                )
            )
            assert absent["error"].startswith("NOT_FOUND:")
            assert absent["details"] == {"missing_ids": [missing]}
            dependency = envelope(
                await client.call_tool("kb_delete", {"kind": kind, "ids": [ids[0]]}, raise_on_error=False)
            )
            assert dependency["error"] == "CONFLICT: DEPENDENCIES_EXIST"
            assert "blocking_record" in dependency["details"]
            job_id = str(uuid4())

            def hold(connection, _token):
                JobStore.admit_delete(connection, DeleteRequest(kind=kind, ids=[ids[0]], cascade=True), job_id)
                return JobStore.claim(connection, "short", "delete")

            claim = await kb.workers.write(hold)
            assert claim is not None
            before = envelope(await client.call_tool("kb_jobs", {}))["data"]["jobs"]
            for selection in ([ids[1], ids[0]], [ids[0]], [ids[0]]):
                pending = envelope(
                    await client.call_tool(
                        "kb_delete", {"kind": kind, "ids": selection, "cascade": True}, raise_on_error=False
                    )
                )
                assert pending["error"] == "CONFLICT: RECORD_DELETING"
                details = pending["details"]
                assert details["delete_job_id"] == job_id
                assert details["blocking_record"] == {"kind": kind, "id": ids[0]}
                assert details["pending_since"]
                assert "reused" not in pending
            after = envelope(await client.call_tool("kb_jobs", {}))["data"]["jobs"]
            assert len(after) == len(before)
            ready = envelope(await client.call_tool("kb_get", {"kind": kind, "ids": [ids[1]]}))["data"]["records"][0]
            assert ready["lifecycle"] == "ready"
            inspected = envelope(await client.call_tool("kb_get", {"kind": kind, "ids": [ids[0]]}))["data"]["records"][
                0
            ]
            assert inspected["delete_job_id"] == job_id
            cancel = envelope(
                await client.call_tool("kb_jobs", {"action": "cancel", "job_id": job_id}, raise_on_error=False)
            )
            assert cancel["error"] == "CONFLICT: DELETE_ALREADY_COMMITTED"
            await client.call_tool("kb_write", {"nodes": [{"id": nodes[2], "label": "ready endpoint"}]})
            if kind == "nodes":
                blocker = envelope(
                    await client.call_tool("kb_delete", {"kind": "nodes", "ids": [nodes[2]]}, raise_on_error=False)
                )["details"]
                assert blocker["blocking_record"] == {"kind": "relations", "id": relations[0]}
                assert blocker["deletion_owner"] == {"kind": "nodes", "id": nodes[0]}
                assert blocker["delete_job_id"] == job_id
                assert blocker["pending_since"] == details["pending_since"]
                types = envelope(await client.call_tool("kb_types", {"kind": "relations", "type": "subdomain_of"}))[
                    "data"
                ]
                assert types["types"][0]["count"] == 1
                for patch in (
                    {"id": relations[0]},
                    {"id": relations[0], "source_ref": {"id": nodes[0]}},
                    {"id": relations[0], "target_ref": {"id": nodes[2]}},
                ):
                    rejected = envelope(
                        await client.call_tool("kb_write", {"relations": [patch]}, raise_on_error=False)
                    )
                    assert rejected["error"] == "CONFLICT: RECORD_DELETING"
                    assert rejected["details"] == details
            if kind == "evidence":
                rejected = envelope(
                    await client.call_tool(
                        "kb_write", {"nodes": [{"id": nodes[2], "evidence_add": [evidence[0]]}]}, raise_on_error=False
                    )
                )
                assert rejected["error"] == "CONFLICT: RECORD_DELETING"
                assert rejected["details"] == details
            await kb.workers.control(
                lambda connection, _token: JobStore.finish(connection, claim, "failed", {"error": "IO_ERROR"})
            )
        # A real client disconnect/reconnect must not replace delete intent or job.
        async with client_for(tmp_path, transport) as client:
            failed = envelope(await client.call_tool("kb_jobs", {"action": "get", "job_id": job_id}))["data"]
            assert failed["retention_protected"] is True
            assert failed["state"] == "failed"
            retried = envelope(await client.call_tool("kb_jobs", {"action": "retry", "job_id": job_id}))["data"]
            assert retried["job_id"] == job_id
            final = retried
            for _ in range(200):
                final = envelope(await client.call_tool("kb_jobs", {"action": "get", "job_id": job_id}))["data"]
                if final["state"] == "completed":
                    break
                await asyncio.sleep(0.01)
            assert final["state"] == "completed"
            assert envelope(await client.call_tool("kb_get", {"kind": kind, "ids": [ids[0]]}))["data"][
                "missing_ids"
            ] == [ids[0]]
            neighbor = envelope(await client.call_tool("kb_get", {"kind": "nodes", "ids": [nodes[2]]}))["data"][
                "records"
            ][0]
            assert neighbor["label"] == "ready endpoint"
            links = envelope(await client.call_tool("kb_get", {"kind": "nodes", "ids": [nodes[1]], "view": "links"}))[
                "data"
            ]["links"]
            assert links == [evidence[1]]
            if kind != "evidence":
                assert (
                    envelope(await client.call_tool("kb_read_evidence", {"evidence_id": evidence[0]}))["data"][
                        "content"
                    ]
                    == "linked-0"
                )


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_response_budget_and_lifetime_link_pagination(tmp_path, transport):

    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        graph = await kb.write(
            {
                "nodes": [
                    {"type": "hostname", "properties": {"name": f"large-{index}", "blob": "x" * 60000}}
                    for index in range(5)
                ]
            }
        )
        ids = [entry["id"] for entry in graph["nodes"]]
        evidence = []
        for index in range(151):
            job = await kb.ingest_evidence(
                {"text": str(index), "targets": [{"kind": "nodes", "id": ids[0]}], "source": "first"}
            )
            evidence.append(job["evidence_id"])
        for source in ("second", "third"):
            await kb.ingest_evidence({"text": "0", "source": source})
        async with client_for(tmp_path, transport) as client:
            record = envelope(await client.call_tool("kb_get", {"kind": "nodes", "ids": ids}))
            assert len(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode()) <= 256 * 1024
            assert record["data"]["remaining_ids"] == ids[4:]
            assert len(record["data"]["records"]) == 4
            seen, cursor = [], None
            while True:
                page = envelope(
                    await client.call_tool(
                        "kb_get", {"kind": "nodes", "ids": [ids[0]], "view": "links", "limit": 20, "cursor": cursor}
                    )
                )["data"]
                seen.extend(page["links"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
                wrong = envelope(
                    await client.call_tool(
                        "kb_get",
                        {"kind": "nodes", "ids": [ids[1]], "view": "links", "cursor": cursor},
                        raise_on_error=False,
                    )
                )
                assert wrong["error"].startswith("INVALID:")
            assert seen == evidence
            sources, cursor = [], None
            while True:
                page = envelope(
                    await client.call_tool(
                        "kb_get",
                        {"kind": "evidence", "ids": [evidence[0]], "view": "sources", "limit": 1, "cursor": cursor},
                    )
                )["data"]
                sources.extend(entry["source"] for entry in page["sources"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
                wrong = envelope(
                    await client.call_tool(
                        "kb_get",
                        {"kind": "evidence", "ids": [evidence[1]], "view": "sources", "cursor": cursor},
                        raise_on_error=False,
                    )
                )
                assert wrong["error"].startswith("INVALID:")
            assert sources == ["first", "second", "third"]
            controls = envelope(await client.call_tool("kb_ingest_evidence", {"text": "\x01" * 65536}))["data"][
                "evidence_id"
            ]
            too_large = envelope(
                await client.call_tool(
                    "kb_read_evidence", {"evidence_id": controls, "length": 65536}, raise_on_error=False
                )
            )
            assert too_large["error"].startswith("LIMIT:")
            exact = envelope(
                await client.call_tool(
                    "kb_read_evidence", {"evidence_id": controls, "length": 65536, "format": "base64"}
                )
            )["data"]["content"]
            assert base64.b64decode(exact) == b"\x01" * 65536


async def test_two_stdio_and_http_share_commits_after_disconnect_and_kill(tmp_path):

    async with client_for(tmp_path, "stdio") as first, client_for(tmp_path, "http") as http:
        async with client_for(tmp_path, "stdio") as second:
            written = envelope(
                await first.call_tool(
                    "kb_write",
                    {
                        "nodes": [
                            {
                                "type": "hostname",
                                "properties": {"name": "shared.example", "values": [None, 1.25, True, "Ä"]},
                            }
                        ]
                    },
                )
            )["data"]
            identifier = written["nodes"][0]["id"]
            request = {"kind": "nodes", "ids": [identifier]}
            # One shared record has identical IDs/timestamps: compare whole
            # transport outputs without any product or test data normalization.
            assert envelope(await second.call_tool("kb_get", request)) == envelope(
                await http.call_tool("kb_get", request)
            )
            assert (
                envelope(await http.call_tool("kb_search", {"kind": "nodes"}))["data"]["items"][0]["id"] == identifier
            )
        await first.call_tool("kb_write", {"nodes": [{"id": identifier, "label": "after disconnect"}]})
        async with process(tmp_path) as killed:
            await initialize(killed)
            result = await wire_request(
                killed,
                {
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "tools/call",
                    "params": {
                        "name": "kb_write",
                        "arguments": {"nodes": [{"id": identifier, "source": "committed before kill"}]},
                    },
                },
            )
            assert result["result"]["structuredContent"]["status"] == "ok"
            killed.kill()
            await killed.wait()
        record = envelope(await http.call_tool("kb_get", request))["data"]["records"][0]
        assert record["label"] == "after disconnect"
        assert record["source"] == "committed before kill"
        await first.call_tool("kb_write", {"nodes": [{"id": identifier, "properties": {"after_kill": True}}]})
        assert (
            envelope(await http.call_tool("kb_get", request))["data"]["records"][0]["properties"]["after_kill"] is True
        )


@pytest.mark.parametrize(
    "mode",
    ["normal", "partial-setup", "lifespan", "normal-lifespan", "cancelled-lifespan", "cancelled-read", "flush-error"],
)
async def test_stdio_adapter_restores_owned_descriptors_and_flags(tmp_path, mode):

    probe = """
import os,fcntl,json,asyncio,sys
from contextlib import asynccontextmanager
import anyio
from justpen_knowledgebase_mcp.stdio import _wire_streams, KnowledgeBaseMCP
original=os.fstat
flags=[fcntl.fcntl(fd,fcntl.F_GETFL) for fd in (0,1)]
identities=[os.fstat(fd).st_ino for fd in (0,1)]
def descriptors():
    found=[]
    for fd in range(64):
        try: fcntl.fcntl(fd,fcntl.F_GETFD); found.append(fd)
        except OSError: pass
    return found
before=descriptors()
calls=0
def failure(fd):
    global calls
    calls+=1
    if calls==2:
        print("accidental startup output")
        raise OSError("injected partial setup")
    return original(fd)
mode=os.environ["TEST_MODE"]
if mode=="partial-setup": os.fstat=failure
@asynccontextmanager
async def broken_lifespan(server):
    print("accidental startup output")
    if mode=="lifespan":
        raise OSError("injected lifespan startup")
    if mode=="cancelled-lifespan":
        asyncio.get_running_loop().call_later(.05, asyncio.current_task().cancel)
    try:
        yield
    finally:
        print("accidental buffered teardown output")
async def cancelled_read():
    with _wire_streams() as (reader, writer):
        print("accidental startup output")
        with anyio.fail_after(.05):
            await reader.readline()
output=sys.stdout
flush_attempts=0
flush_error=None
class FlushFailure:
    def flush(self):
        global flush_attempts
        flush_attempts+=1
        assert os.fstat(1).st_ino==os.fstat(2).st_ino
        raise OSError("injected flush failure")
if mode=="flush-error": sys.stdout=FlushFailure()
try:
    if mode in ("lifespan", "normal-lifespan", "cancelled-lifespan"):
        asyncio.run(KnowledgeBaseMCP("probe",lifespan=broken_lifespan).run_stdio_async())
    elif mode=="cancelled-read":
        asyncio.run(cancelled_read())
        raise AssertionError("partial read did not await cancellation")
    else:
        with _wire_streams():
            if mode!="flush-error": print("accidental startup output")
            assert os.fstat(1).st_ino==os.fstat(2).st_ino
except asyncio.CancelledError:
    assert mode=="cancelled-lifespan"
except TimeoutError:
    assert mode=="cancelled-read"
except OSError as exc:
    assert mode in ("partial-setup", "lifespan", "flush-error")
    if mode=="flush-error": flush_error=str(exc)
finally:
    sys.stdout=output
if mode=="flush-error":
    assert flush_attempts==1
    assert flush_error=="injected flush failure"
assert [fcntl.fcntl(fd,fcntl.F_GETFL) for fd in (0,1)]==flags
assert [os.fstat(fd).st_ino for fd in (0,1)]==identities
assert descriptors()==before
print(json.dumps({"restored":True}),flush=True)
"""
    async with process(tmp_path, probe=probe, env={"TEST_MODE": mode}) as child:
        assert child.stdin is not None
        if mode == "normal-lifespan":
            child.stdin.close()
        else:
            child.stdin.write(b'{"unfinished":')
            await child.stdin.drain()
        await asyncio.wait_for(child.wait(), 5)
        out, err = await child.communicate()
        assert child.returncode == 0, err
        assert json.loads(out) == {"restored": True}
        if mode != "flush-error":
            assert b"accidental startup output" in err
        if mode in ("normal-lifespan", "cancelled-lifespan"):
            assert b"accidental buffered teardown output" in err


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize("code", ["BUSY", "LIMIT"])
async def test_real_lock_contention_and_deadline_errors(tmp_path, transport, code):
    config = {
        "JUSTPEN_KNOWLEDGEBASE_DB_BUSY_TIMEOUT_MS": "50" if code == "BUSY" else "2000",
        "JUSTPEN_KNOWLEDGEBASE_QUERY_TIMEOUT_MS": "1000" if code == "BUSY" else "150",
    }
    async with (
        KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb,
        client_for(tmp_path, transport, env=config) as client,
    ):
        entered, release = threading.Event(), threading.Event()

        def hold(connection, _token):
            entered.set()
            release.wait(timeout=3)
            return connection.execute("SELECT 1").get

        holder = asyncio.create_task(kb.workers.write(hold))
        assert await asyncio.to_thread(entered.wait, 2)
        try:
            status = envelope(await client.call_tool("kb_status", {}))["data"]
            assert status["database"]["available"] is True
            result = envelope(
                await client.call_tool(
                    "kb_write",
                    {"nodes": [{"type": "hostname", "properties": {"name": "blocked.example"}}]},
                    raise_on_error=False,
                )
            )
            assert result["error"].startswith(code + ":")
        finally:
            release.set()
            await holder
        assert envelope(await client.call_tool("kb_search", {"kind": "nodes"}))["data"]["items"] == []
        await client.call_tool(
            "kb_write", {"nodes": [{"type": "hostname", "properties": {"name": "after-lock.example"}}]}
        )


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_wal_busy_and_cached_status_share_retry_guidance(tmp_path, transport):
    async with (
        KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb,
        client_for(tmp_path, transport) as client,
    ):
        descriptor = open_lock(kb.workspace, "checkpoint.lock")
        await asyncio.to_thread(fcntl.flock, descriptor, fcntl.LOCK_EX)

        def pressure(connection, _token):
            original = WalState.read(connection)
            state = original.model_copy(
                update={
                    "phase": "pressure",
                    "pressure_started_at": time.time(),
                    "next_attempt_not_before": time.time() + 5,
                    "last_attempt": "blocked",
                }
            )
            connection.execute("UPDATE settings SET maintenance=?", (state.model_dump_json(),))
            return original

        original = await kb.workers.control(pressure)
        try:
            busy = envelope(await client.call_tool("kb_search", {"kind": "nodes"}, raise_on_error=False))
            assert busy["error"] == "BUSY: WAL_PRESSURE"
            details = busy["details"]
            status = envelope(await client.call_tool("kb_status", {}))["data"]
            assert status["wal"]["phase"] == "pressure"
            assert status["wal"]["reason"] == details["reason"]
            assert (
                max(1000, details["retry_after_ms"] - 500)
                <= status["wal"]["retry_after_ms"]
                <= details["retry_after_ms"]
            )
            assert status["wal"]["estimated_completion_ms"] is None
            assert status["wal"]["pressure_elapsed"] >= 0
            assert status["wal"]["next_attempt_not_before"] is not None
            assert status["wal"]["last_attempt"] == "blocked"
        finally:
            await kb.workers.control(
                lambda connection, _token: (
                    connection.execute("UPDATE settings SET maintenance=?", (original.model_dump_json(),)).get
                )
            )
            os.close(descriptor)
