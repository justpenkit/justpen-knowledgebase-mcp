"""Cancellable stream adapter with SDK and descriptor operations isolated."""

import socket
import stat
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import anyio
import pytest

from justpen_knowledgebase_mcp import stdio


@pytest.fixture
def socket_endpoint(monkeypatch):
    """Model descriptor operations without native resources in the unit suite."""
    endpoint = SimpleNamespace(
        parts=[],
        requests=[],
        flags=2,
        opened=True,
        detached=False,
        outcome=None,
        blocking=True,
        events=[],
        default_timeout=None,
        wrappers=0,
    )

    def send(view, flags=socket.MSG_DONTWAIT):
        assert endpoint.opened
        assert flags == socket.MSG_DONTWAIT
        endpoint.events.append("send")
        endpoint.requests.append(len(view))
        if endpoint.outcome is not None:
            count = endpoint.outcome(view)
        elif len(endpoint.requests) == 2:
            raise BlockingIOError
        else:
            count = min(len(view), 4093 if len(endpoint.requests) == 1 else 65536)
        endpoint.parts.append(bytes(view[:count]))
        return count

    def detach():
        endpoint.detached = True
        return 9

    def change_flags(_value):
        endpoint.flags |= 2048

    def close(fd):
        assert fd == 9
        assert endpoint.opened
        endpoint.opened = False

    async def ready(_fd):
        endpoint.events.append("wait")
        await anyio.lowlevel.checkpoint()

    monkeypatch.setattr(
        stdio,
        "os",
        SimpleNamespace(
            fstat=Mock(return_value=SimpleNamespace(st_mode=stat.S_IFSOCK)),
            fpathconf=Mock(return_value=512),
            read=Mock(return_value=b"line\n"),
            devnull="/dev/null",
            write=lambda _fd, view: send(view),
            set_blocking=lambda _fd, value: change_flags(value),
            get_blocking=lambda _fd: endpoint.blocking,
            close=close,
            dup2=lambda _owned, _descriptor: None,
        ),
    )
    monkeypatch.setattr(stdio.anyio, "wait_writable", ready)
    wrapper = SimpleNamespace(
        send=send, detach=detach, setblocking=change_flags, settimeout=change_flags, getsockopt=lambda *_args: 2048
    )

    def wrap(*, fileno):
        endpoint.wrappers += 1
        if endpoint.default_timeout is not None:
            change_flags(endpoint.default_timeout)
        return wrapper

    monkeypatch.setattr(
        stdio,
        "socket",
        SimpleNamespace(
            socket=wrap,
            getdefaulttimeout=lambda: endpoint.default_timeout,
            MSG_DONTWAIT=socket.MSG_DONTWAIT,
            SOL_SOCKET=socket.SOL_SOCKET,
            SO_SNDLOWAT=socket.SO_SNDLOWAT,
        ),
    )
    monkeypatch.setattr(stdio, "sys", SimpleNamespace(platform="linux"))
    return endpoint


async def test_socket_large_write_uses_bounded_bulk_sends(socket_endpoint):
    payload = ("é" * (150 * 1024)).encode()
    inherited_flags_before = socket_endpoint.flags
    assert await stdio._PipeFile(9).write(payload.decode()) == 150 * 1024
    sent_parts, requested_sizes = socket_endpoint.parts, socket_endpoint.requests
    inherited_flags_after = socket_endpoint.flags
    assert b"".join(sent_parts) == payload
    assert max(requested_sizes) <= 65536
    assert len(requested_sizes) < len(payload) // 100
    assert inherited_flags_after == inherited_flags_before
    assert socket_endpoint.detached
    stdio._restore(1, 9)
    assert not socket_endpoint.opened


@pytest.mark.parametrize("blocked", [False, True])
async def test_socket_cancellation_detaches_before_descriptor_cleanup(socket_endpoint, blocked):
    with anyio.CancelScope() as scope:

        def send(view):
            scope.cancel()
            if blocked:
                raise BlockingIOError
            return len(view)

        socket_endpoint.outcome = send
        await stdio._PipeFile(9).write("x" * (300 * 1024))
        pytest.fail("write did not observe cancellation")
    assert scope.cancelled_caught
    assert socket_endpoint.detached
    assert socket_endpoint.opened
    assert len(socket_endpoint.requests) == 1
    stdio._restore(1, 9)
    assert not socket_endpoint.opened


@pytest.mark.parametrize("blocking", [False, True])
async def test_darwin_socket_respects_write_readiness_low_water(socket_endpoint, monkeypatch, blocking):
    monkeypatch.setattr(stdio.sys, "platform", "darwin")
    socket_endpoint.blocking = blocking
    assert await stdio._PipeFile(9).write("x" * (300 * 1024)) == 300 * 1024
    assert b"".join(socket_endpoint.parts) == b"x" * (300 * 1024)
    assert max(socket_endpoint.requests) == (2048 if blocking else 65536)
    if blocking:
        for index, event in enumerate(socket_endpoint.events):
            if event == "send":
                assert index > 0
                assert socket_endpoint.events[index - 1] == "wait"
    assert socket_endpoint.detached


@pytest.mark.parametrize("default_timeout", [0.0, 1.0])
async def test_socket_global_timeout_preserves_inherited_flags(socket_endpoint, default_timeout):
    socket_endpoint.default_timeout = default_timeout
    inherited_flags_before = socket_endpoint.flags
    assert await stdio._PipeFile(9).write("response") == 8
    assert b"".join(socket_endpoint.parts) == b"response"
    assert socket_endpoint.flags == inherited_flags_before
    assert socket_endpoint.wrappers == 0
    stdio._restore(1, 9)
    assert not socket_endpoint.opened


@pytest.mark.parametrize("failure", [BrokenPipeError, OSError, "zero"])
async def test_socket_failed_send_detaches_and_does_not_retry(socket_endpoint, failure):
    def send(_view):
        if failure == "zero" and len(socket_endpoint.requests) == 1:
            return 0
        raise (BrokenPipeError if failure == "zero" else failure)("peer closed")

    socket_endpoint.outcome = send
    with pytest.raises(BrokenPipeError if failure == "zero" else failure):
        await stdio._PipeFile(9).write("response")
    assert len(socket_endpoint.requests) == 1
    assert socket_endpoint.detached
    stdio._restore(1, 9)
    assert not socket_endpoint.opened


@pytest.mark.parametrize("regular", [False, True])
async def test_pipe_buffer_eof_partial_writes_and_readiness(monkeypatch, regular):
    with monkeypatch.context() as monkeypatch:
        monkeypatch.setattr(
            stdio.os, "fstat", Mock(return_value=SimpleNamespace(st_mode=stat.S_IFREG if regular else stat.S_IFIFO))
        )
        readable, writable = AsyncMock(), AsyncMock()
        monkeypatch.setattr(stdio.os, "fpathconf", Mock(return_value=512))
        monkeypatch.setattr(stdio.anyio, "wait_readable", readable)
        monkeypatch.setattr(stdio.anyio, "wait_writable", writable)
        monkeypatch.setattr(stdio.os, "read", Mock(side_effect=[BlockingIOError(), b"one\ntw", b"o\xff", b""]))
        stream = stdio._PipeFile(9)
        assert await stream.readline() == "one\n"
        assert await stream.readline() == "two�"
        writes = Mock(side_effect=[BlockingIOError(), 1, 2])
        monkeypatch.setattr(stdio.os, "write", writes)
        assert await stream.write("éx") == 2
        assert writes.call_count == 3
        assert readable.call_count == (0 if regular else 4)
        assert writable.call_count == (0 if regular else 3)
        await stream.flush()


@pytest.mark.parametrize(
    ("mode", "rdev", "null_mode", "readiness_count"),
    [
        (stat.S_IFCHR, 31, stat.S_IFCHR, 0),
        (stat.S_IFCHR, 32, stat.S_IFCHR, 1),
        (stat.S_IFCHR, 31, stat.S_IFREG, 1),
        (stat.S_IFREG, 0, stat.S_IFCHR, 0),
        (stat.S_IFIFO, 0, stat.S_IFCHR, 1),
        (stat.S_IFSOCK, 0, stat.S_IFCHR, 1),
    ],
)
async def test_descriptor_identity_routes_checkpoint_or_readiness(
    socket_endpoint, monkeypatch, mode, rdev, null_mode, readiness_count
):
    del socket_endpoint
    descriptor_stat = Mock(return_value=SimpleNamespace(st_mode=mode, st_rdev=rdev))
    read = Mock(return_value=b"line\n")
    monkeypatch.setattr(stdio.os, "fstat", descriptor_stat)
    monkeypatch.setattr(stdio.os, "read", read)
    null_path = Mock(
        return_value=SimpleNamespace(stat=Mock(return_value=SimpleNamespace(st_mode=null_mode, st_rdev=31)))
    )
    monkeypatch.setattr(stdio, "Path", null_path)
    original_checkpoint = anyio.lowlevel.checkpoint
    checkpoints: list[None] = []
    waited: list[int] = []

    async def checkpoint() -> None:
        checkpoints.append(None)
        await original_checkpoint()

    async def readiness(descriptor: int) -> None:
        waited.append(descriptor)
        await original_checkpoint()

    monkeypatch.setattr(stdio.anyio.lowlevel, "checkpoint", checkpoint)
    monkeypatch.setattr(stdio.anyio, "wait_readable", readiness)
    assert await stdio._PipeFile(9).readline() == "line\n"
    assert waited == [9] * readiness_count
    assert len(checkpoints) == 1 - readiness_count
    if stat.S_ISCHR(mode):
        null_path.assert_called_once_with(stdio.os.devnull)
    else:
        null_path.assert_not_called()
    read.assert_called_once_with(9, 65536)


async def test_null_device_read_cancellation_precedes_mocked_read(socket_endpoint, monkeypatch):
    del socket_endpoint
    descriptor_stat = Mock(return_value=SimpleNamespace(st_mode=stat.S_IFCHR, st_rdev=31))
    read = Mock(return_value=b"")
    monkeypatch.setattr(stdio.os, "fstat", descriptor_stat)
    monkeypatch.setattr(stdio.os, "read", read)
    monkeypatch.setattr(
        stdio,
        "Path",
        Mock(return_value=SimpleNamespace(stat=Mock(return_value=SimpleNamespace(st_mode=stat.S_IFCHR, st_rdev=31)))),
    )
    readiness = AsyncMock(side_effect=AssertionError("null device entered readiness"))
    monkeypatch.setattr(stdio.anyio, "wait_readable", readiness)
    stream = stdio._PipeFile(9)
    with anyio.CancelScope() as scope:
        scope.cancel()
        await stream.readline()
        pytest.fail("null-device read skipped its cancellation checkpoint")
    assert scope.cancelled_caught
    read.assert_not_called()
    readiness.assert_not_called()
    assert await stream.readline() == ""
    read.assert_called_once_with(9, 65536)
    readiness.assert_not_called()


def test_restore_closes_duplicate_even_when_duplication_fails(monkeypatch):
    with monkeypatch.context() as monkeypatch:
        duplicate, close = Mock(side_effect=OSError("duplication")), Mock()
        monkeypatch.setattr(stdio.os, "dup2", duplicate)
        monkeypatch.setattr(stdio.os, "close", close)
        with pytest.raises(OSError):
            stdio._restore(0, 7)
        duplicate.assert_called_once_with(7, 0)
        close.assert_called_once_with(7)


def test_wire_streams_restores_stdio_after_body_exception(monkeypatch):
    with monkeypatch.context() as monkeypatch:
        control = Mock(side_effect=[7, 8])
        monkeypatch.setattr(stdio.fcntl, "fcntl", control)
        duplicate, blocking, restore = Mock(), Mock(), Mock()
        monkeypatch.setattr(stdio.os, "dup2", duplicate)
        monkeypatch.setattr(stdio.os, "set_blocking", blocking)
        monkeypatch.setattr(stdio, "_restore", restore)
        monkeypatch.setattr(stdio, "_PipeFile", Mock(side_effect=["input", "output"]))
        monkeypatch.setattr(Path, "open", Mock(return_value=nullcontext(Mock(fileno=Mock(return_value=9)))))
        monkeypatch.setattr(stdio.sys, "stdout", Mock())

        def body():
            with stdio._wire_streams() as streams:
                assert streams == ("input", "output")
                raise ValueError("body")

        with pytest.raises(ValueError, match="body"):
            body()
        assert [call.args for call in restore.call_args_list] == [(1, 8), (0, 7)]
        assert [call.args for call in duplicate.call_args_list] == [(9, 0), (2, 1)]
        blocking.assert_not_called()


async def test_stdio_sdk_adapter_resets_transport_on_failure(monkeypatch):
    @asynccontextmanager
    async def lifespan():
        yield

    @asynccontextmanager
    async def sdk(**streams):
        assert streams == {"stdin": "input", "stdout": "output"}
        yield "reader", "writer"

    monkeypatch.setattr(stdio, "_wire_streams", lambda: nullcontext(("input", "output")))
    monkeypatch.setattr(stdio, "temporary_log_level", lambda _level: nullcontext())
    monkeypatch.setattr(stdio, "stdio_server", sdk)
    reset = Mock()
    monkeypatch.setattr(stdio, "set_transport", Mock(return_value="transport-token"))
    monkeypatch.setattr(stdio, "reset_transport", reset)
    sdk_server = Mock(run=AsyncMock(side_effect=ValueError("sdk failure")))
    mcp = Mock(_lifespan_manager=lifespan, _mcp_server=sdk_server)
    with pytest.raises(ValueError, match="sdk failure"):
        await stdio.KnowledgeBaseMCP.run_stdio_async(mcp)
    assert sdk_server.run.call_args.args[:2] == ("reader", "writer")
    reset.assert_called_once_with("transport-token")
