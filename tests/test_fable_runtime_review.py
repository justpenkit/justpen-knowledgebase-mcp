"""Focused runtime regressions isolated for the Fable production audit."""

import asyncio
import os
import subprocess
import sys
import threading
import time
from contextlib import suppress
from typing import IO, cast

import apsw
import pytest

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import ConfigurationError, ConflictError, PathDeniedError, StorageIOError
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.connection import ManagedConnection, SQLiteRuntime
from justpen_knowledgebase_mcp.storage.maintenance import CheckpointMaintenance, StatusCache, WalState
from justpen_knowledgebase_mcp.workspace import WorkspacePaths


def test_fable_status_cache_does_not_regress_sample_sequence():
    cache = StatusCache()
    cache.update(
        WalState(
            sample_seq=5,
            phase="normal",
            sample_at=time.time(),
            allocated_bytes=0,
            log_frames=0,
            checkpointed_frames=0,
            page_size=4096,
            unbackfilled_bytes=0,
        )
    )
    cache.update(WalState(sample_seq=4, phase="pressure"))
    assert cache.snapshot()["sample_seq"] == 5


@pytest.mark.integration
async def test_fable_maintenance_reports_runtime_failure_without_silent_exit(tmp_path, monkeypatch):
    config = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(config) as workspace, SQLiteRuntime(workspace, config) as factory:
        maintenance = CheckpointMaintenance(factory)
        await maintenance.start()
        original = factory.guard.check
        failed = threading.Event()

        def fail_once(connection):
            if threading.current_thread().name == "kb-checkpoint" and not failed.is_set():
                failed.set()
                raise StorageIOError("injected maintenance storage failure")
            return original(connection)

        monkeypatch.setattr(factory.guard, "check", fail_once)
        try:
            maintenance.request("pressure")
            assert await asyncio.to_thread(failed.wait, 2)
            await asyncio.to_thread(cast("threading.Thread", maintenance._thread).join, 0.2)
            assert (cast("threading.Thread", maintenance._thread).is_alive(), maintenance.status()["last_attempt"]) == (
                True,
                "failed",
            )
        finally:
            with suppress(StorageIOError):
                await maintenance.close()


@pytest.mark.integration
async def test_fable_one_reader_retirement_preserves_healthy_writer(kb, monkeypatch):
    await kb.status_sampler.close()
    await kb.job_runner.close()
    await kb.maintenance.close()
    execute = ManagedConnection.execute
    affected = []
    injected = []
    closed = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_close = SQLiteRuntime.close_connection

    def notify_retirement(connection):
        try:
            return original_close(connection)
        finally:
            if affected and connection is affected[0] and connection.gate.closed:
                loop.call_soon_threadsafe(closed.set)

    def fail_rollback(self, sql, bindings=None, **kwargs):
        if affected and self is affected[0] and sql == "ROLLBACK" and not injected:
            injected.append(True)
            raise apsw.IOError("injected reader rollback failure")
        return execute(self, sql, bindings, **kwargs)

    def fail_read(connection, token):
        affected.append(connection)
        raise ConflictError("original read failure")

    monkeypatch.setattr(ManagedConnection, "execute", fail_rollback)
    monkeypatch.setattr(SQLiteRuntime, "close_connection", staticmethod(notify_retirement))
    with pytest.raises(ConflictError, match="original read failure"):
        await kb.workers.read(fail_read)
    assert injected == [True]
    await asyncio.wait_for(closed.wait(), 2)
    assert affected[0].gate.closed
    assert await kb.workers.write(lambda connection, token: connection.execute("select 7").get) == 7
    assert await kb.workers.read(lambda connection, token: connection.execute("select 8").get) == 8


@pytest.mark.integration
def test_fable_stdio_hard_stop_preserves_inherited_open_file_flags():
    reader, writer = os.pipe()
    process = None
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                "import os,time; from justpen_knowledgebase_mcp.stdio import _wire_streams; "
                "\nwith _wire_streams():\n os.write(2,b'ready\\n'); time.sleep(10)\n",
            ],
            stdin=reader,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        assert cast("IO[bytes]", process.stderr).readline() == b"ready\n"
        process.kill()
        process.wait(timeout=5)
        assert os.get_blocking(reader)
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            cast("IO[bytes]", process.stderr).close()
        os.set_blocking(reader, True)
        os.close(reader)
        os.close(writer)


@pytest.mark.integration
async def test_lifespan_filesystem_ownership_is_off_event_loop(tmp_path, monkeypatch):
    current = threading.get_ident()
    calls = []
    initialize = WorkspacePaths._initialize
    close = WorkspacePaths.close
    runtime_init = SQLiteRuntime.__init__

    def record_initialize(self, config):
        calls.append(("workspace_init", threading.get_ident()))
        initialize(self, config)

    def record_runtime(self, workspace, config):
        calls.append(("runtime_init", threading.get_ident()))
        runtime_init(self, workspace, config)

    def record_close(self):
        calls.append(("workspace_close", threading.get_ident()))
        close(self)

    monkeypatch.setattr(WorkspacePaths, "_initialize", record_initialize)
    monkeypatch.setattr(WorkspacePaths, "close", record_close)
    monkeypatch.setattr(SQLiteRuntime, "__init__", record_runtime)
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)):
        pass
    assert [name for name, _owner in calls] == ["workspace_init", "runtime_init", "workspace_close"]
    assert all(owner != current for _name, owner in calls), calls


@pytest.mark.integration
@pytest.mark.parametrize("phase", ["workspace", "runtime"])
async def test_cancelled_lifespan_initialization_disposes_created_resources(tmp_path, monkeypatch, phase):
    entered, release = threading.Event(), threading.Event()
    created = []
    target = WorkspacePaths if phase == "workspace" else SQLiteRuntime
    initialize = target.__init__

    def delayed(self, *args):
        initialize(self, *args)
        created.append(self)
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(target, "__init__", delayed)

    async def open_service():
        async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)):
            pytest.fail("cancelled startup admitted service")

    task = asyncio.create_task(open_service())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    resource = created[0]
    if phase == "workspace":
        assert resource.root_fd == -1
    else:
        assert resource.vfs.name not in apsw.vfs_names()
        assert resource.workspace.root_fd == -1


@pytest.mark.integration
async def test_transient_maintenance_failure_recovers_on_next_measurement(kb, monkeypatch, caplog):
    original = kb.workers.factory.guard.check
    failed = threading.Event()
    recovered = asyncio.Event()
    publish = CheckpointMaintenance._publish
    loop = asyncio.get_running_loop()

    def fail_once(connection):
        if threading.current_thread().name == "kb-checkpoint" and not failed.is_set():
            failed.set()
            raise StorageIOError("private evidence payload")
        return original(connection)

    def observe_publish(self, connection, state):
        publish(self, connection, state)
        if failed.is_set() and state.phase == "normal":
            loop.call_soon_threadsafe(recovered.set)

    monkeypatch.setattr(kb.workers.factory.guard, "check", fail_once)
    monkeypatch.setattr(CheckpointMaintenance, "_publish", observe_publish)
    kb.maintenance.request("pressure")
    await asyncio.wait_for(recovered.wait(), 4)
    assert kb.maintenance.status()["maintenance_alive"]
    assert kb.maintenance.status()["maintenance_error"] is None
    assert await kb.workers.read(lambda connection, _token: connection.execute("select 1").get) == 1
    assert "private evidence payload" not in caplog.text


@pytest.mark.integration
@pytest.mark.parametrize("fault", [ConfigurationError, PathDeniedError, apsw.CorruptError])
async def test_permanent_maintenance_failure_blocks_products_but_preserves_control(kb, monkeypatch, fault):
    original = kb.workers.factory.guard.check

    def fail_owner(connection):
        if threading.current_thread().name == "kb-checkpoint":
            raise fault("private evidence payload")
        return original(connection)

    monkeypatch.setattr(kb.workers.factory.guard, "check", fail_owner)
    kb.maintenance.request("pressure")
    await asyncio.to_thread(cast("threading.Thread", kb.maintenance._thread).join, 2)
    status = await kb.status()
    assert status["wal"]["maintenance_failed_permanently"]
    assert not status["wal"]["maintenance_alive"]
    expected = StorageIOError if fault is apsw.CorruptError else fault
    for operation in (kb.workers.read, kb.workers.write):
        with pytest.raises(expected, match="maintenance unavailable"):
            await operation(lambda _connection, _token: pytest.fail("permanent failure admitted product"))
    assert await kb.workers.control(lambda connection, _token: connection.execute("select 9").get) == 9


@pytest.mark.integration
async def test_maintenance_repeated_failure_respects_retry_cadence(kb, monkeypatch):
    first, second = threading.Event(), threading.Event()
    calls = []

    def fail_open():
        calls.append(time.monotonic())
        (first if len(calls) == 1 else second).set()
        raise StorageIOError("bounded injected failure")

    monkeypatch.setattr(kb.maintenance, "_open", fail_open)
    kb.maintenance.request("pressure")
    assert await asyncio.to_thread(first.wait, 2)
    kb.maintenance.request("pressure")
    assert await asyncio.to_thread(second.wait, 3)
    await kb.maintenance.close()
    assert len(calls) == 2
    assert calls[1] - calls[0] >= 0.95


@pytest.mark.integration
def test_stdio_large_pipe_output_and_backpressure_remain_cancellable():
    probe = """
import asyncio, os
from justpen_knowledgebase_mcp.stdio import _PipeFile
async def main():
    reader, writer = os.pipe()
    try:
        outgoing = _PipeFile(writer)
        incoming = _PipeFile(reader)
        payload = 'é' * 262144 + '\\n'
        count, received = await asyncio.gather(outgoing.write(payload), incoming.readline())
        assert count == len(payload) and received == payload
        try:
            async with asyncio.timeout(.05):
                await outgoing.write('x' * 1048576)
        except TimeoutError:
            pass
        else:
            raise AssertionError('blocked output was not cancelled')
        assert os.get_blocking(reader) and os.get_blocking(writer)
    finally:
        os.close(reader)
        os.close(writer)
asyncio.run(main())
"""
    completed = subprocess.run([sys.executable, "-B", "-c", probe], capture_output=True, timeout=5, check=False)
    assert completed.returncode == 0, completed.stderr
