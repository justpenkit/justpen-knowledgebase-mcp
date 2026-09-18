"""Real child-process MCP fixtures; no product protocol or storage shortcuts."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx2 as httpx
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


def environment(workspace: Path) -> dict[str, str]:
    """Use explicit test workspace and avoid inherited KB configuration."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("JUSTPEN_KNOWLEDGEBASE_")}
    env.update(
        JUSTPEN_KNOWLEDGEBASE_WORKSPACE_DIR=str(workspace),
        FASTMCP_SHOW_SERVER_BANNER="false",
        PYTHONDONTWRITEBYTECODE="1",
    )
    return env


def free_port() -> int:
    """Reserve an available loopback port for a short-lived test child."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@asynccontextmanager
async def process(
    workspace: Path, *args: str, env: dict[str, str] | None = None, probe: str | None = None
) -> AsyncGenerator[asyncio.subprocess.Process]:
    """Own child pipes and always reap even a failed shutdown regression."""
    command = (
        [sys.executable, "-B", "-c", probe] if probe else [sys.executable, "-B", "-m", "justpen_knowledgebase_mcp"]
    )
    child = await asyncio.create_subprocess_exec(
        *command,
        *args,
        env={**environment(workspace), **(env or {})},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        yield child
    finally:
        if child.returncode is None:
            child.kill()
        await asyncio.wait_for(child.communicate(), 10)


async def wire_request(child: asyncio.subprocess.Process, message: dict[str, Any]) -> dict[str, Any]:
    """Exchange a raw request while requiring stdout to remain JSON protocol."""
    assert child.stdin is not None
    assert child.stdout is not None
    child.stdin.write((json.dumps(message) + "\n").encode())
    await child.stdin.drain()
    while True:
        line = await asyncio.wait_for(child.stdout.readline(), 10)
        assert line, "server closed protocol output"
        response = json.loads(line)
        if response.get("id") == message.get("id"):
            return response


async def initialize(child: asyncio.subprocess.Process) -> None:
    """Exercise SDK initialization before testing live transport shutdown."""
    response = await wire_request(
        child,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "kb-test", "version": "1"},
            },
        },
    )
    assert "result" in response
    assert child.stdin is not None
    child.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    await child.stdin.drain()


@asynccontextmanager
async def http_server(
    workspace: Path,
    *,
    host: str = "127.0.0.1",
    allow: bool = False,
    probe: str | None = None,
    env: dict[str, str] | None = None,
) -> AsyncGenerator[tuple[str, asyncio.subprocess.Process]]:
    """Start a real HTTP listener, preserving stderr and process ownership."""
    port = free_port()
    address = "[::1]" if host == "::1" else "127.0.0.1"
    url = f"http://{address}:{port}/mcp"
    settings = {"JUSTPEN_KNOWLEDGEBASE_ALLOW_NON_LOOPBACK": str(allow).lower(), **(env or {})}
    async with process(
        workspace, "--transport", "http", "--host", host, "--port", str(port), env=settings, probe=probe
    ) as child:
        async with httpx.AsyncClient() as http:
            for _ in range(200):
                if child.returncode is not None:
                    assert child.stderr is not None
                    raise AssertionError((await child.stderr.read()).decode())
                try:
                    await http.get(url)
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.025)
            else:
                raise AssertionError("HTTP listener did not become ready")
        yield url, child
        child.terminate()
        await asyncio.wait_for(child.wait(), 10)


@asynccontextmanager
async def client_for(
    workspace: Path, transport: str, *, env: dict[str, str] | None = None
) -> AsyncGenerator[Client[Any]]:
    """Use the actual SDK client on either supported process transport."""
    if transport == "http":
        async with http_server(workspace, env=env) as (url, _child), Client(url) as client:
            yield client
    else:
        with (workspace / "stdio-stderr.log").open("w") as log:
            transport_owner = StdioTransport(
                sys.executable,
                ["-B", "-m", "justpen_knowledgebase_mcp"],
                env={**environment(workspace), **(env or {})},
                keep_alive=False,
                log_file=log,
            )
            async with Client(transport_owner) as client:
                yield client


class WireClient:
    """Raw fixtures share only transport framing, never a product JSON parser."""

    def __init__(self, child: asyncio.subprocess.Process | None = None, url: str | None = None) -> None:
        self.child = child
        self.url = url
        self.http = httpx.AsyncClient(timeout=15)
        self.headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        self.pending: dict[str | int, asyncio.Future[dict[str, Any]]] = {}
        self.reader: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.child is not None:
            self.reader = asyncio.create_task(self._read())
        await self.request(
            1,
            "initialize",
            {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "raw-test", "version": "1"}},
        )
        await self.notify("notifications/initialized", {})

    async def _read(self) -> None:
        assert self.child is not None
        assert self.child.stdout is not None
        while line := await self.child.stdout.readline():
            response = json.loads(line)
            future = self.pending.get(response.get("id"))
            if future is not None and not future.done():
                future.set_result(response)

    async def raw(self, identifier: str | int | None, body: bytes) -> dict[str, Any]:
        if self.child is not None:
            assert self.child.stdin is not None
            future = asyncio.get_running_loop().create_future()
            if identifier is not None:
                self.pending[identifier] = future
            self.child.stdin.write(body + b"\n")
            await self.child.stdin.drain()
            if identifier is None:
                return {}
            try:
                return await asyncio.wait_for(future, 12)
            finally:
                self.pending.pop(identifier, None)
        assert self.url is not None
        response = await self.http.post(self.url, headers=self.headers, content=body)
        response.raise_for_status()
        if "mcp-session-id" in response.headers:
            self.headers["mcp-session-id"] = response.headers["mcp-session-id"]
            self.headers["MCP-Protocol-Version"] = "2025-11-25"
        if not response.content:
            return {}
        if "text/event-stream" in response.headers.get("content-type", ""):
            values = [json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith("data:")]
            return next(value for value in values if value.get("id") == identifier)
        return response.json()

    async def request(self, identifier: str | int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self.raw(
            identifier, json.dumps({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}).encode()
        )

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self.raw(None, json.dumps({"jsonrpc": "2.0", "method": method, "params": params}).encode())

    async def close(self) -> None:
        if self.reader is not None:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        await self.http.aclose()


@asynccontextmanager
async def wire_client(workspace: Path, transport: str, *, probe: str | None = None) -> AsyncGenerator[WireClient]:
    if transport == "http":
        async with http_server(workspace, probe=probe) as (url, _child):
            client = WireClient(url=url)
            try:
                await client.start()
                yield client
            finally:
                await client.close()
    else:
        async with process(workspace, probe=probe) as child:
            client = WireClient(child=child)
            try:
                await client.start()
                yield client
            finally:
                await client.close()
                if child.returncode is None:
                    child.terminate()
                    await asyncio.wait_for(child.wait(), 10)


CANCELLATION_PROBE = """
import os,time
from pathlib import Path
from justpen_knowledgebase_mcp.storage.graph import Graph
from justpen_knowledgebase_mcp.__main__ import cli
root=Path(os.environ["JUSTPEN_KNOWLEDGEBASE_WORKSPACE_DIR"])
original=Graph.write
def write(connection, token, request):
    result=original(connection, token, request)
    if request.nodes and request.nodes[0].properties.get("value")=="cancel.example":
        mode=(root/"mode").read_text()
        def barrier():
            (root/"entered").write_text(token.state)
            deadline=time.monotonic()+8
            while not (root/"release").exists() and time.monotonic()<deadline:
                time.sleep(.005)
        if mode=="precommit":
            barrier()
        else:
            fired=False
            def hook():
                nonlocal fired
                if not fired:
                    fired=True
                    barrier()
                return False
            connection.set_commit_hook(hook)
    return result
Graph.write=staticmethod(write)
cli()
"""


async def wait_file(path: Path) -> None:
    for _ in range(300):
        if await asyncio.to_thread(path.exists):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"test barrier not reached: {path.name}")
