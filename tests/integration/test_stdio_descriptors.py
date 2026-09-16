"""Native descriptor identity and readiness behavior for stdio streams."""

import os
import socket
from contextlib import ExitStack

import anyio
import pytest

from justpen_knowledgebase_mcp import stdio

pytestmark = pytest.mark.integration


async def test_null_character_device_returns_eof_without_readiness_and_checkpoints(monkeypatch):
    async def no_readiness(_descriptor: int) -> None:
        pytest.fail("null device must not enter descriptor readiness")

    monkeypatch.setattr(stdio.anyio, "wait_readable", no_readiness)
    descriptor = os.open(os.devnull, os.O_RDONLY)
    try:
        stream = stdio._PipeFile(descriptor)
        with anyio.CancelScope() as scope:
            scope.cancel()
            await stream.readline()
            pytest.fail("null-device read skipped its cancellation checkpoint")
        assert scope.cancelled_caught
        assert await stream.readline() == ""
    finally:
        os.close(descriptor)


async def test_other_character_device_still_waits_for_readiness(monkeypatch):
    async def readiness(_descriptor: int) -> None:
        raise LookupError("readiness path")

    monkeypatch.setattr(stdio.anyio, "wait_readable", readiness)
    descriptor = os.open("/dev/zero", os.O_RDONLY)
    try:
        with pytest.raises(LookupError, match="readiness path"):
            await stdio._PipeFile(descriptor).readline()
    finally:
        os.close(descriptor)


@pytest.mark.parametrize(("kind", "readiness_count"), [("regular", 0), ("fifo", 1), ("socket", 1)])
async def test_non_null_descriptors_keep_readiness_behavior(monkeypatch, tmp_path, kind, readiness_count):
    original_wait_readable = stdio.anyio.wait_readable
    waited: list[int] = []

    async def record_readiness(descriptor: int) -> None:
        waited.append(descriptor)
        await original_wait_readable(descriptor)

    monkeypatch.setattr(stdio.anyio, "wait_readable", record_readiness)
    with ExitStack() as stack:
        if kind == "regular":
            path = tmp_path / "input.txt"
            path.write_bytes(b"line\n")
            descriptor = os.open(path, os.O_RDONLY)
            stack.callback(os.close, descriptor)
        elif kind == "fifo":
            descriptor, writer = os.pipe()
            stack.callback(os.close, descriptor)
            os.write(writer, b"line\n")
            os.close(writer)
        else:
            reader, writer = socket.socketpair()
            stack.callback(reader.close)
            stack.callback(writer.close)
            writer.sendall(b"line\n")
            descriptor = reader.fileno()

        assert await stdio._PipeFile(descriptor).readline() == "line\n"
        assert waited == [descriptor] * readiness_count
