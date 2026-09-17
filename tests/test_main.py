"""Verify CLI exit status, server failures, and graceful task cleanup."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import anyio
import pytest

import justpen_knowledgebase_mcp.__main__ as main_mod
import justpen_knowledgebase_mcp.service as service_mod
import justpen_knowledgebase_mcp.shutdown as shutdown_mod
from justpen_knowledgebase_mcp.errors import ConfigurationError, PathDeniedError, StorageIOError

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastmcp import FastMCP

    from justpen_knowledgebase_mcp.telemetry.runtime import TelemetryRuntime


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


@pytest.mark.parametrize(
    "reason",
    ["offline workspace upgrade required", "CONFIGURATION: workspace unavailable"],
)
def test_cli_formats_known_runtime_configuration_once(monkeypatch: pytest.MonkeyPatch, capsys, reason: str) -> None:
    async def fail_startup(_config: main_mod.ServerConfig) -> None:
        raise ConfigurationError(reason)

    monkeypatch.setattr(main_mod, "parse_config", lambda: main_mod.ServerConfig(workspace_dir=Path("/workspace")))
    monkeypatch.setattr(main_mod, "main", fail_startup)
    with pytest.raises(SystemExit) as raised:
        main_mod.cli()
    assert raised.value.code == 2
    assert capsys.readouterr().err == f"CONFIGURATION: {reason.removeprefix('CONFIGURATION: ')}\n"


async def test_main_runs_to_completion_when_server_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    capture_signals(monkeypatch)
    before = asyncio.all_tasks()

    async def quick_exit(**_kwargs) -> None:
        return

    monkeypatch.setattr(main_mod, "create_app", lambda config, **kwargs: SimpleNamespace(run_async=quick_exit))
    await main_mod.main()
    assert asyncio.all_tasks() == before


@pytest.mark.parametrize(
    ("error", "code", "message"),
    [
        (PathDeniedError("PATH_DENIED: SYMLINK_COMPONENT"), 2, "PATH_DENIED: SYMLINK_COMPONENT"),
        (StorageIOError("IO_ERROR: MANAGED_DIRECTORY_CHANGED"), 1, "IO_ERROR: MANAGED_DIRECTORY_CHANGED"),
        (StorageIOError("managed storage failed"), 1, "IO_ERROR: managed storage failed"),
    ],
)
def test_cli_formats_known_application_failures_without_causes(monkeypatch, capsys, error, code, message):
    async def fail_startup(_config):
        raise error from OSError("SECRET-MARKER")

    monkeypatch.setattr(main_mod, "parse_config", lambda: main_mod.ServerConfig(workspace_dir=Path("/workspace")))
    monkeypatch.setattr(main_mod, "main", fail_startup)
    with pytest.raises(SystemExit) as raised:
        main_mod.cli()
    assert raised.value.code == code
    assert raised.value.__suppress_context__
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == message + "\n"


def test_cli_preserves_unexpected_failures(monkeypatch):
    async def fail_startup(_config):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(main_mod, "parse_config", lambda: main_mod.ServerConfig(workspace_dir=Path("/workspace")))
    monkeypatch.setattr(main_mod, "main", fail_startup)
    with pytest.raises(RuntimeError, match="unexpected failure"):
        main_mod.cli()


async def test_http_launch_wires_explicit_hosts_with_strict_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    capture_signals(monkeypatch)
    captured: dict[str, Any] = {}

    async def quick_exit(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(main_mod, "create_app", lambda config, **kwargs: SimpleNamespace(run_async=quick_exit))
    config = main_mod.ServerConfig(
        workspace_dir=tmp_path,
        transport="http",
        host="0.0.0.0",  # noqa: S104 - assert explicit wildcard binding with a fixed Host alias
        allow_non_loopback=True,
        allowed_hosts=("kb.example.test",),
    )
    await main_mod._serve(config, cast("TelemetryRuntime", SimpleNamespace(asgi_middleware=lambda: None)))
    assert captured["host_origin_protection"] is True
    assert captured["allowed_hosts"] == [config.host, "kb.example.test"]


@pytest.mark.parametrize("stop_requested", [False, True], ids=["server-failure", "simultaneous-stop-and-failure"])
async def test_main_propagates_server_failure(monkeypatch: pytest.MonkeyPatch, *, stop_requested: bool) -> None:
    handlers = capture_signals(monkeypatch)
    before = asyncio.all_tasks()
    failure = RuntimeError("server startup failed")

    async def failing_server(**_kwargs) -> None:
        if stop_requested:
            handlers[signal.SIGTERM]()
        raise failure

    monkeypatch.setattr(main_mod, "create_app", lambda config, **kwargs: SimpleNamespace(run_async=failing_server))
    with pytest.raises(RuntimeError, match="server startup failed") as raised:
        await main_mod.main()
    assert raised.value is failure
    assert asyncio.all_tasks() == before


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_signal_waits_for_server_cleanup(
    monkeypatch: pytest.MonkeyPatch, sig: signal.Signals, transport: str
) -> None:
    handlers = capture_signals(monkeypatch)
    monkeypatch.setenv("JUSTPEN_KNOWLEDGEBASE_TRANSPORT", transport)
    before = asyncio.all_tasks()
    cleaned_up = asyncio.Event()

    async def running_server(**_kwargs) -> None:
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

    async def running_server(**_kwargs) -> None:
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


@pytest.mark.parametrize("trigger", ["signal", "main-cancel"])
async def test_stdio_stop_cancels_relay_before_receiver_teardown(monkeypatch, trigger):
    handlers = capture_signals(monkeypatch)
    pending = asyncio.Event()
    relayed = asyncio.Event()
    failures = []
    before = asyncio.all_tasks()

    async def running_server(**_kwargs):
        send, receive = anyio.create_memory_object_stream[str]()

        async def relay():
            pending.set()
            try:
                await send.send("pending frame")
            except anyio.BrokenResourceError as error:
                failures.append(error)
                raise
            finally:
                relayed.set()

        async with send, anyio.create_task_group() as tasks:
            tasks.start_soon(relay)
            try:
                await asyncio.Event().wait()
            finally:
                # The SDK dispatcher closes its input before the enclosing
                # replay task group exits. Yield at that exact shutdown window.
                receive.close()
                await anyio.lowlevel.cancel_shielded_checkpoint()

    monkeypatch.setattr(main_mod, "create_app", lambda config, **kwargs: SimpleNamespace(run_async=running_server))
    config = main_mod.ServerConfig(workspace_dir=Path("/workspace"))
    task = asyncio.create_task(main_mod._serve(config, cast("TelemetryRuntime", None)))
    await pending.wait()
    if trigger == "signal":
        handlers[signal.SIGTERM]()
        await task
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert relayed.is_set()
    assert failures == []
    assert asyncio.all_tasks() == before


async def test_stdio_cancel_scope_remembers_stop_before_server_entry():
    scope = anyio.CancelScope()
    scope.cancel()
    cleaned = asyncio.Event()

    async def running_server(**_kwargs):
        try:
            await anyio.lowlevel.checkpoint()
            pytest.fail("a pre-cancelled server cannot accept work")
        finally:
            cleaned.set()

    server = cast("FastMCP", SimpleNamespace(run_async=running_server))
    await asyncio.create_task(main_mod._run_stdio(server, scope))
    assert cleaned.is_set()
    assert scope.cancelled_caught


@pytest.mark.parametrize("mixed", [False, True])
async def test_stdio_stop_preserves_genuine_transport_errors(monkeypatch, mixed):
    handlers = capture_signals(monkeypatch)
    broken = anyio.BrokenResourceError()
    failure = ExceptionGroup("SDK failures", [broken, ValueError("server failure")]) if mixed else broken
    cleaned = asyncio.Event()

    async def running_server(**_kwargs):
        try:
            handlers[signal.SIGTERM]()
            await asyncio.Event().wait()
        finally:
            cleaned.set()
            raise failure

    monkeypatch.setattr(main_mod, "create_app", lambda config, **kwargs: SimpleNamespace(run_async=running_server))
    config = main_mod.ServerConfig(workspace_dir=Path("/workspace"))
    with pytest.raises(type(failure)) as raised:
        await main_mod._serve(config, cast("TelemetryRuntime", None))
    assert raised.value is failure
    assert cleaned.is_set()


@pytest.mark.integration
def test_cli_exits_with_failure_when_server_crashes() -> None:
    probe = """
from justpen_knowledgebase_mcp import __main__ as entrypoint

async def failing_server(**_kwargs):
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


@pytest.mark.integration
@pytest.mark.parametrize("trigger", ["signal", "server-return", "startup-failure"])
async def test_shutdown_timeout_logs_and_keeps_waiting_for_cleanup(monkeypatch, caplog, tmp_path, trigger):
    handlers = capture_signals(monkeypatch)
    release = asyncio.Event()
    closing = asyncio.Event()
    config = main_mod.ServerConfig(workspace_dir=tmp_path)
    original_close = service_mod.DatabaseWorkers.close

    async def slow_close(workers):
        closing.set()
        await release.wait()
        await original_close(workers)

    async def running_server(observer):
        async with service_mod.KnowledgeBase.open(config, _shutdown_observer=observer):
            if trigger == "signal":
                handlers[signal.SIGTERM]()
                await asyncio.Event().wait()

    if trigger == "startup-failure":

        def fail_reader(factory):
            raise RuntimeError("reader startup failed")

        monkeypatch.setattr(service_mod.SQLiteRuntime, "open_reader", fail_reader)
    monkeypatch.setattr(service_mod.DatabaseWorkers, "close", slow_close)
    monkeypatch.setattr(
        main_mod,
        "create_app",
        lambda config, **kwargs: SimpleNamespace(
            run_async=lambda **_run_kwargs: running_server(kwargs["_shutdown_observer"])
        ),
    )
    monkeypatch.setattr(shutdown_mod, "SHUTDOWN_GRACE_SECONDS", 0.01)
    task = asyncio.create_task(main_mod.main(config))
    try:
        await asyncio.wait_for(closing.wait(), timeout=5)
        await asyncio.sleep(0.04)
        assert caplog.text.count("shutdown_timeout") == 1
        assert not task.done()
    finally:
        release.set()
        if trigger == "startup-failure":
            with pytest.raises(RuntimeError, match="reader startup failed"):
                await task
        else:
            await task
    assert caplog.text.count("shutdown_timeout") == 1


@pytest.mark.integration
@pytest.mark.parametrize("trigger", ["signal", "eof", "idle-stdin-signal"])
def test_supervisor_hard_stop_recovers_committed_wal(tmp_path, trigger):
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
from justpen_knowledgebase_mcp.storage.connection import ManagedConnection
native_init=ManagedConnection.__init__
native_close=ManagedConnection.close_native
def initialize(self, factory, *, reader=False):
    global live_count
    native_init(self, factory, reader=reader)
    with closing_lock:
        live_count+=1
        if live_count==1:
            self.execute("insert into nodes(uuid,type,key,properties) values ('committed','ip','a','{}')")
        if live_count==4 and sys.argv[2]!='signal':
            print('ready',file=sys.stderr,flush=True)
def close_native(self, *, force=False):
    global live_count
    with closing_lock:
        live_count-=1
        last=live_count==0
    if last:
        print('closing',file=sys.stderr,flush=True)
        time.sleep(60)
    return native_close(self,force=force)
# Keep the actual factory/gate and owner-thread lifetime; delay only native close.
ManagedConnection.__init__=initialize
ManagedConnection.close_native=close_native
config=ServerConfig(workspace_dir=__import__('pathlib').Path(sys.argv[1]),log_level='ERROR')
import justpen_knowledgebase_mcp.shutdown as shutdown
shutdown.SHUTDOWN_GRACE_SECONDS=.05
import fastmcp
fastmcp.settings.show_server_banner=False
if sys.argv[2]=='signal':
    # Retain the original controlled signal scenario; EOF below exercises the
    # actual FastMCP stdio transport, not a stand-in reading stdin.
    async def server(observer):
        async with KnowledgeBase.open(config, _shutdown_observer=observer):
            print('ready',file=sys.stderr,flush=True)
            await asyncio.Event().wait()
    entry.create_app=lambda config, **kwargs:SimpleNamespace(run_async=lambda **_run_kwargs:server(kwargs["_shutdown_observer"]))
asyncio.run(entry.main(config))
"""
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", probe, str(tmp_path), trigger],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stderr is not None
        assert select.select([process.stderr], [], [], 10)[0]
        assert process.stderr.readline().strip() == "ready"
        if trigger != "signal":
            # Native connections precede lifespan readiness. Initialize the real
            # transport before testing an idle signal or normal EOF teardown.
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "clientInfo": {"name": "slow-close-probe", "version": "1"},
                        },
                    }
                )
                + "\n"
            )
            process.stdin.flush()
            assert select.select([process.stdout], [], [], 10)[0]
            assert "result" in json.loads(process.stdout.readline())
        if trigger != "eof":
            process.send_signal(signal.SIGTERM)
        else:
            assert process.stdin is not None
            process.stdin.close()
            process.stdin = None
        # The timeout line arrives while native owner cleanup is still pending.
        stderr_seen = b""
        deadline = time.monotonic() + 10
        while b"shutdown_timeout" not in stderr_seen or b"closing" not in stderr_seen:
            assert select.select([process.stderr], [], [], max(0, deadline - time.monotonic()))[0], stderr_seen
            chunk = os.read(process.stderr.fileno(), 4096)
            assert chunk, stderr_seen
            stderr_seen += chunk
        assert stderr_seen.count(b"shutdown_timeout") == 1
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


@pytest.mark.parametrize("trigger", ["signal", "main-cancel"])
async def test_signal_timeout_before_lifespan_cleanup_logs_once(monkeypatch, caplog, trigger):
    handlers = capture_signals(monkeypatch)
    release = asyncio.Event()
    entered = asyncio.Event()

    async def server(**_kwargs):
        try:
            entered.set()
            if trigger == "signal":
                handlers[signal.SIGTERM]()
            await asyncio.Event().wait()
        finally:
            # Model a transport teardown still waiting before lifespan unwinds.
            with anyio.CancelScope(shield=True):
                await release.wait()

    monkeypatch.setattr(main_mod, "create_app", lambda config, **kwargs: SimpleNamespace(run_async=server))
    monkeypatch.setattr(shutdown_mod, "SHUTDOWN_GRACE_SECONDS", 0.01)
    task = asyncio.create_task(main_mod.main())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        if trigger == "main-cancel":
            task.cancel()
        await asyncio.sleep(0.04)
        assert caplog.text.count("shutdown_timeout") == 1
        assert not task.done()
    finally:
        release.set()
        if trigger == "main-cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
    assert caplog.text.count("shutdown_timeout") == 1
