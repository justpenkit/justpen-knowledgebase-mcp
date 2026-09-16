"""POSIX cancellable streams at the pinned FastMCP/SDK transport boundary.

FastMCP does not expose SDK stream injection. Only run_stdio_async is adapted;
SDK stdio_server remains responsible for every JSON-RPC parse and write. No
worker-thread readline survives transport shutdown. Inherited file flags are never
changed. The protocol owns exclusive consumption of its inherited stream endpoints.
"""

from __future__ import annotations

import fcntl
import io
import os
import socket
import stat
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from fastmcp import FastMCP
from fastmcp.server.context import reset_transport, set_transport
from fastmcp.utilities.logging import temporary_log_level
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.stdio import stdio_server
from typing_extensions import override

if TYPE_CHECKING:
    from collections.abc import Generator


class _PipeFile(anyio.AsyncFile[str]):
    """SDK-compatible line/text stream using cancellable descriptor readiness."""

    def __init__(self, descriptor: int) -> None:
        super().__init__(io.StringIO())
        self._descriptor = descriptor
        descriptor_stat = os.fstat(descriptor)
        mode = descriptor_stat.st_mode
        self._regular = stat.S_ISREG(mode)
        if stat.S_ISCHR(mode):
            null_stat = Path(os.devnull).stat()
            self._null_device = stat.S_ISCHR(null_stat.st_mode) and descriptor_stat.st_rdev == null_stat.st_rdev
        else:
            self._null_device = False
        self._socket = stat.S_ISSOCK(mode)
        self._write_size = (
            os.fpathconf(descriptor, "PC_PIPE_BUF") if stat.S_ISFIFO(mode) else 65536 if self._regular else 1
        )
        self._buffer = bytearray()

    @override
    async def readline(self) -> str:
        while b"\n" not in self._buffer:
            if self._regular or self._null_device:
                await anyio.lowlevel.checkpoint()
            else:
                await anyio.wait_readable(self._descriptor)
            try:
                chunk = os.read(self._descriptor, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                result = bytes(self._buffer)
                self._buffer.clear()
                return result.decode("utf-8", errors="replace")
            self._buffer.extend(chunk)
        ending = self._buffer.index(b"\n") + 1
        result = bytes(self._buffer[:ending])
        del self._buffer[:ending]
        return result.decode("utf-8", errors="replace")

    @override
    async def write(self, b: str) -> int:
        remaining = memoryview(b.encode("utf-8"))
        # Constructing a socket with a process-wide default timeout can change
        # the shared OFD flags. Embedders with that setting retain the cautious
        # descriptor path; callers must not race global socket configuration.
        if self._socket and socket.getdefaulttimeout() is None:
            await self._write_socket(remaining)
            return len(b)
        while remaining:
            if not self._regular:
                await anyio.wait_writable(self._descriptor)
            else:
                await anyio.lowlevel.checkpoint()
            try:
                written = os.write(self._descriptor, remaining[: self._write_size])
            except BlockingIOError:
                continue
            remaining = remaining[written:]
        return len(b)

    async def _write_socket(self, remaining: memoryview) -> None:
        # The wrapper temporarily owns the duplicate, but _wire_streams retains
        # responsibility for restoring and closing it. Never change OFD flags:
        # socket.setblocking/settimeout would also affect the inherited endpoint.
        sock = socket.socket(fileno=self._descriptor)
        try:
            # Darwin's MSG_DONTWAIT does not prevent waiting for buffer space.
            # With an exclusive writer, readiness guarantees SO_SNDLOWAT bytes.
            wait_first = sys.platform == "darwin" and os.get_blocking(self._descriptor)
            write_size = (
                min(65536, max(1, sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDLOWAT))) if wait_first else 65536
            )
            await anyio.lowlevel.checkpoint()
            while remaining:
                if wait_first:
                    await anyio.wait_writable(self._descriptor)
                try:
                    written = sock.send(remaining[:write_size], socket.MSG_DONTWAIT)
                except BlockingIOError:
                    await anyio.wait_writable(self._descriptor)
                else:
                    if written == 0:
                        raise BrokenPipeError("Socket stdio write made no progress")
                    remaining = remaining[written:]
                    await anyio.lowlevel.checkpoint()
        finally:
            sock.detach()

    @override
    async def flush(self) -> None:
        await anyio.lowlevel.checkpoint()


def _restore(descriptor: int, owned: int) -> None:
    try:
        os.dup2(owned, descriptor)
    finally:
        os.close(owned)


@contextmanager
def _wire_streams() -> Generator[tuple[_PipeFile, _PipeFile]]:
    """Isolate startup/tool stdout and restore even partially initialized claims."""
    with ExitStack() as stack:
        descriptors: list[int] = []
        for descriptor in (0, 1):
            owned = fcntl.fcntl(descriptor, fcntl.F_DUPFD_CLOEXEC, 3)
            stack.callback(_restore, descriptor, owned)
            descriptors.append(owned)
        with Path(os.devnull).open("rb") as empty:
            os.dup2(empty.fileno(), 0)
        os.dup2(2, 1)
        # Drain Python text buffering while fd 1 still targets stderr. ExitStack
        # restores descriptors even if this flush raises during unwind.
        stack.callback(sys.stdout.flush)
        yield _PipeFile(descriptors[0]), _PipeFile(descriptors[1])


class KnowledgeBaseMCP(FastMCP):
    """Adapt only stdio stream acquisition; retain SDK protocol and lifecycle."""

    @override
    async def run_stdio_async(
        self, show_banner: bool = True, log_level: str | None = None, stateless: bool = False
    ) -> None:
        # Match the pinned FastMCP signature. Its stateless argument is likewise
        # not passed to the SDK run; stdout remains protocol-only without banners.
        del show_banner, stateless
        token = set_transport("stdio")
        try:
            with temporary_log_level(log_level), _wire_streams() as (stdin, stdout):
                async with self._lifespan_manager(), stdio_server(stdin=stdin, stdout=stdout) as (reader, writer):
                    await self._mcp_server.run(
                        reader,
                        writer,
                        self._mcp_server.create_initialization_options(
                            notification_options=NotificationOptions(tools_changed=True)
                        ),
                    )
        finally:
            reset_transport(token)
