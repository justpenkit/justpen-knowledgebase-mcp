"""Native socket stdio throughput, inherited flags, and cancellable ownership."""

import asyncio
import fcntl
import json
import signal
import socket
import sys
import time

import pytest

from .mcp_client import environment, process

pytestmark = pytest.mark.integration

# Run native writes in a child so even a blocking-write regression has a bounded
# parent deadline. Spies delegate each send/write to its real kernel operation.
TRANSFER_PROBE = """
import asyncio, fcntl, json, os, select, socket, time
from contextlib import ExitStack
import anyio
from justpen_knowledgebase_mcp import stdio

PAYLOAD = ("é" * (150 * 1024)).encode()
mode = os.environ["TEST_SOCKET_MODE"]
requests, counts, detached = [], [], []
blocked_count = 0
real_socket, real_write, real_wait = socket.socket, os.write, anyio.wait_writable

class CountingSocket(real_socket):
    def send(self, view, flags=0):
        requests.append(len(view))
        assert flags == socket.MSG_DONTWAIT
        count = super().send(view, flags)
        counts.append(count)
        return count
    def detach(self):
        detached.append(self.fileno())
        return super().detach()

def write(fd, view):
    requests.append(len(view))
    count = real_write(fd, view)
    counts.append(count)
    return count

def descriptors():
    found = set()
    for fd in range(256):
        try:
            fcntl.fcntl(fd, fcntl.F_GETFD)
            found.add(fd)
        except OSError:
            pass
    return found

async def run():
    global blocked_count
    # Initialize the backend before the descriptor baseline.
    await anyio.lowlevel.checkpoint()
    before = descriptors()
    blocked, finished = anyio.Event(), anyio.Event()
    async def writable(fd):
        global blocked_count
        blocked_count += 1
        if not select.select([], [fd], [], 0)[1]:
            blocked.set()
        await real_wait(fd)
    anyio.wait_writable = writable
    received = bytearray()
    with ExitStack() as stack:
        if mode == "fifo":
            reader, inherited = os.pipe()
            stack.callback(os.close, reader)
            stack.callback(os.close, inherited)
            limit = os.fpathconf(inherited, "PC_PIPE_BUF")
            # Darwin exposes a kernel FWASWRITTEN bit after the first write.
            # Warm the pipe before checking that adapter writes preserve flags.
            real_write(inherited, b"!")
            assert os.read(reader, 1) == b"!"
            stdio.os.write = write
        else:
            owner, peer = socket.socketpair()
            stack.enter_context(owner)
            stack.enter_context(peer)
            owner.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
            if "nonblocking" in mode:
                owner.setblocking(False)
            inherited, reader, limit = owner.fileno(), peer.fileno(), 65536
            stdio.socket.socket = CountingSocket
        flags_before = fcntl.fcntl(inherited, fcntl.F_GETFL)
        duplicate = os.dup(inherited)
        stack.callback(os.close, duplicate)
        stream = stdio._PipeFile(duplicate)
        async def read():
            while len(received) < len(PAYLOAD):
                await anyio.wait_readable(reader)
                received.extend(os.read(reader, 4096))
                if mode == "slow":
                    await anyio.sleep(.001)
        cancelled = mode.startswith("cancel")
        if mode == "broken":
            peer.close()
        scope = anyio.CancelScope()
        async def send():
            try:
                with scope:
                    if mode == "broken":
                        try:
                            await stream.write(PAYLOAD.decode())
                        except BrokenPipeError:
                            pass
                        else:
                            raise AssertionError("broken peer did not fail")
                    else:
                        assert await stream.write(PAYLOAD.decode()) == 150 * 1024
            finally:
                finished.set()
        started = time.monotonic()
        async with anyio.create_task_group() as group:
            group.start_soon(send)
            if cancelled:
                await blocked.wait()
                if mode == "cancel-slow":
                    # Release a little capacity then cancel with output pending.
                    received.extend(os.read(reader, 1024))
                    await anyio.sleep(.01)
                cancel_started = time.monotonic()
                scope.cancel()
                await finished.wait()
                cancel_seconds = time.monotonic() - cancel_started
            elif mode != "broken":
                group.start_soon(read)
        elapsed = time.monotonic() - started
        flags_after = fcntl.fcntl(inherited, fcntl.F_GETFL)
        assert flags_after == flags_before, (flags_before, flags_after)
        assert fcntl.fcntl(duplicate, fcntl.F_GETFL) == flags_before
        assert max(requests) <= limit
        assert len(requests) < len(PAYLOAD) // 100
        if mode == "fifo":
            assert len(requests) == (len(PAYLOAD) + limit - 1) // limit
        else:
            assert detached == [duplicate]
        if cancelled:
            assert scope.cancelled_caught
            while True:
                try:
                    received.extend(peer.recv(65536, socket.MSG_DONTWAIT))
                except BlockingIOError:
                    break
            assert bytes(received) == PAYLOAD[:sum(counts)]
            assert 0 < len(received) < len(PAYLOAD)
            sends_after_cancel = len(requests)
            await anyio.sleep(.02)
            assert len(requests) == sends_after_cancel
            try:
                peer.recv(65536, socket.MSG_DONTWAIT)
            except BlockingIOError:
                pass
            else:
                raise AssertionError("write survived cancellation")
        elif mode == "broken":
            assert len(requests) == 1 and counts == []
        else:
            assert bytes(received) == PAYLOAD
    # Both wrapper and duplicate are gone; no retained native owner remains.
    assert descriptors() == before
    result = dict(mode=mode, bytes=len(PAYLOAD), calls=len(requests), max_request=max(requests),
                  write_seconds=elapsed, readiness_waits=blocked_count, flags_preserved=True)
    if cancelled:
        result.update(cancel_seconds=cancel_seconds, sent_bytes=len(received))
    print(json.dumps(result), flush=True)

asyncio.run(run())
"""


@pytest.mark.parametrize(
    "mode", ["blocking", "nonblocking", "slow", "fifo", "cancel", "cancel-slow", "cancel-nonblocking", "broken"]
)
async def test_native_stdio_bulk_write_and_cancellation(tmp_path, capsys, mode):
    async with process(tmp_path, probe=TRANSFER_PROBE, env={"TEST_SOCKET_MODE": mode}) as child:
        out, err = await asyncio.wait_for(child.communicate(), 15)
        assert child.returncode == 0, err.decode(errors="replace")
        result = json.loads(out)
        assert result["bytes"] == 307200
        assert result["flags_preserved"]
        with capsys.disabled():
            print(f"\nNative stdio measurement: {json.dumps(result, sort_keys=True)}")


@pytest.mark.parametrize("blocking", [False, True])
async def test_cli_tools_list_over_inherited_socket_stdio(tmp_path, capsys, blocking):
    parent, inherited = socket.socketpair()
    with parent, inherited:
        inherited.setblocking(blocking)
        flags_before = fcntl.fcntl(inherited, fcntl.F_GETFL)
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            "-m",
            "justpen_knowledgebase_mcp",
            env=environment(tmp_path),
            stdin=inherited.fileno(),
            stdout=inherited.fileno(),
            stderr=asyncio.subprocess.PIPE,
        )
        writer = None
        try:
            reader, writer = await asyncio.open_connection(sock=parent, limit=1024 * 1024)

            async def request(message):
                writer.write((json.dumps(message) + "\n").encode())
                await writer.drain()
                while True:
                    line = await asyncio.wait_for(reader.readline(), 15)
                    assert line, "server closed socket stdio"
                    response = json.loads(line)
                    if response.get("id") == message["id"]:
                        return response, len(line)

            initialized, _ = await request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "socket-stdio-test", "version": "1"},
                    },
                }
            )
            assert "result" in initialized
            writer.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            await writer.drain()
            started = time.monotonic()
            response, size = await request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            elapsed = time.monotonic() - started
            assert {tool["name"] for tool in response["result"]["tools"]} >= {"kb_status", "kb_ingest_evidence"}
            child.send_signal(signal.SIGTERM)
            await asyncio.wait_for(child.wait(), 15)
            assert child.stderr is not None
            stderr = await child.stderr.read()
            assert child.returncode == 0, stderr.decode(errors="replace")
            assert b"shutdown_timeout" not in stderr
            assert fcntl.fcntl(inherited, fcntl.F_GETFL) == flags_before
            with capsys.disabled():
                print(f"\nSocket tools/list: blocking={blocking}, bytes={size}, seconds={elapsed:.6f}")
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            if child.returncode is None:
                child.kill()
            await asyncio.wait_for(child.communicate(), 10)
