"""Verify CLI exit status, server failures, and graceful task cleanup."""

from __future__ import annotations

import asyncio
import logging
import os
import select
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

import justpen_knowledgebase_mcp.__main__ as main_mod

if TYPE_CHECKING:
    from collections.abc import Callable


def capture_signals(monkeypatch: pytest.MonkeyPatch) -> dict[signal.Signals, Callable[[], None]]:
    monkeypatch.setenv("JUSTPEN_KNOWLEDGEBASE_WORKSPACE_DIR", "/workspace")
    handlers: dict[signal.Signals, Callable[[], None]] = {}

    def add_handler(sig: signal.Signals, callback: Callable[[], None]) -> None:
        handlers[sig] = callback

    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", add_handler)

    return handlers


def test_setup_logging_resolves_named_level(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_basic_config(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("justpen_knowledgebase_mcp.__main__.logging.basicConfig", fake_basic_config)
    main_mod._setup_logging("DEBUG")
    assert captured["level"] == logging.DEBUG


def test_setup_logging_falls_back_on_unknown_level(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_basic_config(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("justpen_knowledgebase_mcp.__main__.logging.basicConfig", fake_basic_config)
    main_mod._setup_logging("NOT_A_LEVEL")
    assert captured["level"] == logging.INFO


def test_cli_invokes_asyncio_run(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[object] = []

    def fake_run(coro: object) -> None:
        captured.append(coro)
        if hasattr(coro, "close"):
            coro.close()  # type: ignore[attr-defined]

    monkeypatch.setattr("justpen_knowledgebase_mcp.__main__.asyncio.run", fake_run)
    monkeypatch.setattr(main_mod, "parse_config", lambda: main_mod.ServerConfig(workspace_dir=Path("/workspace")))
    main_mod.cli()
    assert len(captured) == 1


async def test_main_runs_to_completion_when_server_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    capture_signals(monkeypatch)
    before = asyncio.all_tasks()

    async def quick_exit() -> None:
        return

    monkeypatch.setattr(main_mod, "create_app", lambda config, **kwargs: SimpleNamespace(run_async=quick_exit))
    await main_mod.main()
    assert asyncio.all_tasks() == before


@pytest.mark.parametrize("stop_requested", [False, True], ids=["server-failure", "simultaneous-stop-and-failure"])
async def test_main_propagates_server_failure(monkeypatch: pytest.MonkeyPatch, *, stop_requested: bool) -> None:
    handlers = capture_signals(monkeypatch)
    before = asyncio.all_tasks()
    failure = RuntimeError("server startup failed")

    async def failing_server() -> None:
        if stop_requested:
            handlers[signal.SIGTERM]()
        raise failure

    monkeypatch.setattr(main_mod, "create_app", lambda config, **kwargs: SimpleNamespace(run_async=failing_server))
    with pytest.raises(RuntimeError, match="server startup failed") as raised:
        await main_mod.main()
    assert raised.value is failure
    assert asyncio.all_tasks() == before


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_signal_waits_for_server_cleanup(monkeypatch: pytest.MonkeyPatch, sig: signal.Signals) -> None:
    handlers = capture_signals(monkeypatch)
    before = asyncio.all_tasks()
    cleaned_up = asyncio.Event()

    async def running_server() -> None:
        try:
            handlers[sig]()
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    monkeypatch.setattr(main_mod, "create_app", lambda config, **kwargs: SimpleNamespace(run_async=running_server))
    await asyncio.wait_for(main_mod.main(), timeout=5)
    assert cleaned_up.is_set()
    assert asyncio.all_tasks() == before


async def test_cancelling_main_cleans_up_server(monkeypatch: pytest.MonkeyPatch) -> None:
    capture_signals(monkeypatch)
    before = asyncio.all_tasks()
    started = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def running_server() -> None:
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    monkeypatch.setattr(main_mod, "create_app", lambda config, **kwargs: SimpleNamespace(run_async=running_server))
    task = asyncio.create_task(main_mod.main())
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned_up.is_set()
    assert asyncio.all_tasks() == before


@pytest.mark.integration
def test_cli_exits_with_failure_when_server_crashes() -> None:
    probe = """
from justpen_knowledgebase_mcp import __main__ as entrypoint

async def failing_server():
    raise RuntimeError("server startup failed")

from types import SimpleNamespace
entrypoint.create_app = lambda config, **kwargs: SimpleNamespace(run_async=failing_server)
entrypoint.cli()
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={**os.environ, "JUSTPEN_KNOWLEDGEBASE_WORKSPACE_DIR": "/workspace"},
    )
    assert result.returncode != 0, result.stderr
    assert "RuntimeError: server startup failed" in result.stderr
    assert "Task exception was never retrieved" not in result.stderr


async def test_shutdown_timeout_logs_and_keeps_waiting_for_cleanup(monkeypatch, caplog):
    handlers = capture_signals(monkeypatch)
    release = asyncio.Event()

    async def running_server():
        try:
            handlers[signal.SIGTERM]()
            await asyncio.Event().wait()
        finally:
            await release.wait()

    monkeypatch.setattr(main_mod, "create_app", lambda config, **kwargs: SimpleNamespace(run_async=running_server))
    monkeypatch.setattr(main_mod, "SHUTDOWN_GRACE_SECONDS", 0.01)
    task = asyncio.create_task(main_mod.main())
    try:
        await asyncio.sleep(0.04)
        assert "shutdown_timeout" in caplog.text
        assert not task.done()
    finally:
        release.set()
    await task


@pytest.mark.integration
def test_supervisor_hard_stop_recovers_committed_wal(tmp_path):
    probe = """
import asyncio, os, signal, sys, time
from justpen_knowledgebase_mcp import __main__ as entry
from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.connection import SQLiteRuntime
from types import SimpleNamespace
import threading
closing_lock=threading.Lock()
live_count=0
class SlowCloseConnection(__import__('apsw').Connection):
    def close(self, *args, **kwargs):
        global live_count
        with closing_lock:
            live_count-=1
            last=live_count==0
        if last:
            print('closing',flush=True)
            time.sleep(60)
        return super().close(*args, **kwargs)
# All native connections retain production VFS/configuration; only last-close
# timing is delayed to model a slow checkpoint/fsync deterministically.
def connect(self):
    global live_count
    connection=SlowCloseConnection(str(self.workspace.db),vfs=self.vfs.name)
    self._configure(connection)
    with closing_lock:
        live_count+=1
    return connection
SQLiteRuntime.connect=connect
config=ServerConfig(workspace_dir=__import__('pathlib').Path(sys.argv[1]))
async def server():
    async with KnowledgeBase.open(config) as kb:
        await kb.workers.write(lambda c,t:c.execute("insert into nodes(uuid,type,key,properties) values ('committed','ip','a','{}')"))
        print('ready',flush=True)
        await asyncio.Event().wait()
entry.create_app=lambda config, **kwargs:SimpleNamespace(run_async=server)
entry.SHUTDOWN_GRACE_SECONDS=.05
asyncio.run(entry.main(config))
"""
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", probe, str(tmp_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        assert process.stdout is not None
        assert select.select([process.stdout], [], [], 10)[0]
        assert process.stdout.readline().strip() == "ready"
        process.send_signal(signal.SIGTERM)
        assert select.select([process.stdout], [], [], 10)[0]
        assert process.stdout.readline().strip() == "closing"
        assert process.stderr is not None
        # The timeout line arrives while native owner cleanup is still pending.
        assert select.select([process.stderr], [], [], 10)[0]
        assert "shutdown_timeout" in process.stderr.readline()
        assert process.poll() is None
        process.kill()
        process.communicate(timeout=5)
        assert (tmp_path / ".justpen/knowledgebase/graph.sqlite3-wal").exists()
        recovery = """
import asyncio,sys
from pathlib import Path
from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.service import KnowledgeBase
async def main():
    async with KnowledgeBase.open(ServerConfig(workspace_dir=Path(sys.argv[1]))) as kb:
        assert await kb.workers.read(lambda c,t:c.execute("select uuid from nodes").get)=='committed'
        await kb.workers.write(lambda c,t:c.execute("update nodes set metadata='{}'"))
asyncio.run(main())
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", recovery, str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
