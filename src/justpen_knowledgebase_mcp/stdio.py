"""POSIX cancellable streams at the pinned FastMCP/SDK transport boundary.

FastMCP does not expose SDK stream injection. Only run_stdio_async is adapted;
SDK stdio_server remains responsible for every JSON-RPC parse and write. No
worker-thread readline survives transport shutdown. Duplicates share file flags,
so flags and standard descriptors are restored before owned descriptors close.
"""

from __future__ import annotations

import fcntl
import io
import os
import stat
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
        self._regular = stat.S_ISREG(os.fstat(descriptor).st_mode)
        self._buffer = bytearray()

    @override
    async def readline(self) -> str:
        while b"\n" not in self._buffer:
            if not self._regular:
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
        while remaining:
            if not self._regular:
                await anyio.wait_writable(self._descriptor)
            try:
                written = os.write(self._descriptor, remaining)
            except BlockingIOError:
                continue
            remaining = remaining[written:]
        return len(b)

    @override
    async def flush(self) -> None:
        await anyio.lowlevel.checkpoint()


def _restore(descriptor: int, owned: int, flags: int) -> None:
    try:
        try:
            fcntl.fcntl(owned, fcntl.F_SETFL, flags)
        finally:
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
            try:
                flags = fcntl.fcntl(owned, fcntl.F_GETFL)
            except BaseException:
                os.close(owned)
                raise
            stack.callback(_restore, descriptor, owned, flags)
            descriptors.append(owned)
        with Path(os.devnull).open("rb") as empty:
            os.dup2(empty.fileno(), 0)
        os.dup2(2, 1)
        for owned in descriptors:
            os.set_blocking(owned, False)
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
