"""Cancellable stream adapter with SDK and descriptor operations isolated."""

import stat
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from justpen_knowledgebase_mcp import stdio


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
