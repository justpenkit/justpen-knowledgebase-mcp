"""Rolling native clients and the reset transaction gate under real WAL maintenance."""

import asyncio
import threading
import time
from contextlib import AsyncExitStack

import pytest

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import BusyError
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.connection import ManagedConnection
from justpen_knowledgebase_mcp.storage.worker import OperationToken

from .mcp_client import wire_client

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("lane", ["read", "write", "control"])
async def test_gate_is_already_held_at_barrier_immediately_before_native_begin(kb, monkeypatch, lane):
    entered, release = threading.Event(), threading.Event()
    peer = kb.workers.factory.open_maintenance()
    token = OperationToken(time.monotonic() + 5)
    original = ManagedConnection.execute

    def execute(connection, statement, *args, **kwargs):
        if statement.startswith("BEGIN") and connection.gate.active_token is token:
            entered.set()
            assert release.wait(3)
        return original(connection, statement, *args, **kwargs)

    monkeypatch.setattr(ManagedConnection, "execute", execute)
    task = asyncio.create_task(getattr(kb.workers, lane)(lambda c, _t: c.execute("select 42").get, token))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(BusyError), peer.gate.reset_window("opportunistic"):
            pytest.fail("reset entered between admission and native BEGIN")
    finally:
        release.set()
        peer.close()
    assert await task == 42


STATUS_PROBE = """
from justpen_knowledgebase_mcp.__main__ import cli
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.connection import ManagedConnection
inside_status=False
original_status=KnowledgeBase.status
original_execute=ManagedConnection.execute
async def status(self):
    global inside_status
    inside_status=True
    try:
        return await original_status(self)
    finally:
        inside_status=False
def execute(self,*args,**kwargs):
    assert not inside_status, 'kb_status attempted SQL during reset'
    return original_execute(self,*args,**kwargs)
KnowledgeBase.status=status
ManagedConnection.execute=execute
cli()
"""


async def test_eight_rolling_get_processes_drain_for_restart_and_status_is_sql_free(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    native = ManagedConnection.wal_checkpoint
    modes = []

    def checkpoint(self, dbname=None, mode=0):
        modes.append(mode)
        if mode == 2:
            entered.set()
            assert release.wait(15), "test failed to release native checkpoint barrier"
        return native(self, dbname, mode)

    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb, AsyncExitStack() as stack:
        owner = (await kb.write({"nodes": [{"type": "hostname", "properties": {"name": "rolling.example"}}]}))["nodes"][
            0
        ]["id"]
        clients = [
            await stack.enter_async_context(wire_client(tmp_path, "stdio", probe=STATUS_PROBE)) for _ in range(8)
        ]
        success_events = [asyncio.Event() for _ in range(8)]
        successes = [0] * 8
        busy = [0] * 8
        stop = asyncio.Event()

        async def rolling(index, client):
            sequence = 0
            while not stop.is_set():
                sequence += 1
                response = await client.request(
                    f"read-{sequence}", "tools/call", {"name": "kb_get", "arguments": {"kind": "nodes", "ids": [owner]}}
                )
                value = response["result"]["structuredContent"]
                if value["status"] == "ok":
                    assert value["data"]["records"][0]["id"] == owner
                    successes[index] += 1
                    success_events[index].set()
                else:
                    assert value["error"].startswith(("BUSY:", "LIMIT:")), value
                    busy[index] += 1
                await asyncio.sleep(0)

        tasks = [asyncio.create_task(rolling(index, client)) for index, client in enumerate(clients)]
        try:
            async with asyncio.timeout(10):
                await asyncio.gather(*(event.wait() for event in success_events))
            await kb.workers.control(
                lambda c, t: c.execute(
                    "update settings set maintenance=json_set(maintenance,'$.next_attempt_not_before',0.0)"
                )
            )
            monkeypatch.setattr(ManagedConnection, "wal_checkpoint", checkpoint)
            kb.maintenance.request("pressure")
            assert await asyncio.to_thread(entered.wait, 5)
            # Native I/O is deliberately longer than the 500ms admission drain budget.
            await asyncio.sleep(0.6)
            responses = await asyncio.gather(
                *(client.request("status", "tools/call", {"name": "kb_status", "arguments": {}}) for client in clients)
            )
            for response in responses:
                value = response["result"]["structuredContent"]
                assert value["status"] == "ok", value
                assert value["data"]["wal"]["estimated_completion_ms"] is None
            release.set()
            for event in success_events:
                event.clear()
            async with asyncio.timeout(12):
                await asyncio.gather(*(event.wait() for event in success_events))
            assert 2 in modes
            assert kb.maintenance.status()["last_attempt"] == "restart"
        finally:
            release.set()
            stop.set()
            await asyncio.gather(*tasks)
