"""Real scoped flock admission and factory ownership regressions."""

import asyncio
import fcntl
import os
import subprocess
import sys
import time

import apsw
import pytest

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import BusyError, LimitError
from justpen_knowledgebase_mcp.storage.connection import ManagedConnection, SQLiteRuntime
from justpen_knowledgebase_mcp.storage.maintenance import WalState
from justpen_knowledgebase_mcp.storage.worker import OperationToken
from justpen_knowledgebase_mcp.workspace import WorkspacePaths

pytestmark = pytest.mark.integration


def test_factory_connections_own_independent_persistent_gate_descriptors(tmp_path):
    cfg = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(cfg) as ws, SQLiteRuntime(ws, cfg) as runtime:
        first, second = runtime.connect(), runtime.connect()
        try:
            assert hasattr(first, "gate"), "factory must create an owner gate before native open"
            token = OperationToken(time.monotonic() + 2)
            descriptors = first.gate.intent_fd, first.gate.transactions_fd
            with first.gate.transaction(token):
                with pytest.raises(RuntimeError, match="scope"), first.gate.transaction(token):
                    pass
                with pytest.raises(BusyError), second.gate.reset_window("opportunistic"):
                    pass
                # Failed second EX releases intent immediately, allowing another reader.
                with second.gate.transaction(OperationToken(time.monotonic() + 1)):
                    assert second.execute("select 1").get == 1
            for _ in range(3):
                with first.gate.transaction(OperationToken(time.monotonic() + 1)):
                    assert (first.gate.intent_fd, first.gate.transactions_fd) == descriptors
            with second.gate.reset_window("opportunistic") as window:
                assert window.busy_timeout_ms == 0
                with pytest.raises(BusyError), first.gate.transaction(OperationToken(time.monotonic() + 0.02)):
                    pytest.fail("SQL must not begin")
        finally:
            second.close()
            first.close()
        for fd in descriptors:
            with pytest.raises(OSError):
                os.fstat(fd)


def test_factory_open_and_close_wait_for_reset_without_native_sql(tmp_path):
    cfg = ServerConfig(workspace_dir=tmp_path, db_busy_timeout_ms=20)
    with WorkspacePaths(cfg) as ws, SQLiteRuntime(ws, cfg) as runtime:
        connection = runtime.connect()
        assert hasattr(connection, "gate"), "close must use factory gate"
        lock = os.open(ws.locks / "reset-intent.lock", os.O_RDWR)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(BusyError):
                runtime.open_reader()
            with pytest.raises(BusyError):
                connection.close()
            assert not connection.gate.closed
        finally:
            os.close(lock)
            connection.close()


async def test_worker_holds_gate_through_callback_and_rollback(kb):
    observed = []

    def callback(connection, token):
        assert hasattr(connection, "gate"), "worker connection must carry its admission scope"
        assert connection.gate.active_token is token
        fd = os.open(kb.workspace.locks / "db-transactions.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        observed.append(True)

    await kb.workers.control(callback)
    assert observed == [True]


def test_native_close_error_is_surfaced_after_gated_force_cleanup(tmp_path, monkeypatch):

    cfg = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(cfg) as ws, SQLiteRuntime(ws, cfg) as runtime:
        connection = runtime.connect()
        assert hasattr(connection, "close_native"), "native close must preserve cleanup ownership on failure"
        original = ManagedConnection.close_native
        calls = []

        def fail_once(self, *, force=False):
            assert self.gate.active
            calls.append(force)
            if len(calls) == 1:
                raise OSError("injected native close failure")
            original(self, force=force)

        monkeypatch.setattr(ManagedConnection, "close_native", fail_once)
        with pytest.raises(OSError, match="injected native close"):
            connection.close()
        assert calls == [False, True]
        assert connection.gate.closed


async def test_worker_shutdown_retries_gate_busy_before_releasing_resources(kb):
    # Match service shutdown order: no background owner should race the test's lock.
    await kb.status_sampler.close()
    await kb.job_runner.close()
    await kb.maintenance.close()
    lock = os.open(kb.workspace.locks / "reset-intent.lock", os.O_RDWR)
    try:
        for owner in kb.workers._owners:
            owner.connection.close_timeout_ms = 10
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        shutdown = asyncio.create_task(kb.workers.close())
        await asyncio.sleep(0.05)
        assert not shutdown.done(), "gate contention must not abandon owner resources"
        assert kb.workspace.root_fd >= 0
    finally:
        os.close(lock)
    outcomes = await asyncio.gather(*(owner.closed for owner in kb.workers._owners), return_exceptions=True)
    assert not [error for error in outcomes if isinstance(error, BaseException)], outcomes
    await shutdown
    assert all(owner.closed.done() for owner in kb.workers._owners)


def test_pressure_drain_budget_and_opportunistic_skip(small_wal):
    factory, _maintenance, writer = small_wal
    reader = factory.open_reader()
    try:
        with reader.gate.transaction(OperationToken(time.monotonic() + 3)):
            start = time.monotonic()
            with pytest.raises(BusyError), writer.gate.reset_window("opportunistic"):
                pytest.fail("reader must prevent reset")
            assert time.monotonic() - start < 0.1
            start = time.monotonic()
            with pytest.raises(BusyError), writer.gate.reset_window("pressure"):
                pytest.fail("reader must prevent reset")
            assert 0.45 <= time.monotonic() - start < 1.0
        with writer.gate.reset_window("pressure") as window:
            assert 0 < window.busy_timeout_ms <= 200
    finally:
        reader.close()


def test_process_kill_releases_leader_intent_and_transaction_locks(tmp_path):

    cfg = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(cfg) as ws, SQLiteRuntime(ws, cfg) as runtime:
        c = runtime.connect()
        script = """
import sys
from pathlib import Path
from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.workspace import WorkspacePaths
from justpen_knowledgebase_mcp.storage.connection import SQLiteRuntime
from justpen_knowledgebase_mcp.storage.admission import open_lock
import fcntl
cfg=ServerConfig(workspace_dir=Path(sys.argv[1]))
with WorkspacePaths(cfg) as ws, SQLiteRuntime(ws,cfg) as factory:
    c=factory.open_maintenance()
    leader=open_lock(ws,'checkpoint.lock')
    fcntl.flock(leader,fcntl.LOCK_EX)
    with c.gate.reset_window('pressure'):
        print('locked',flush=True)
        sys.stdin.read()
"""
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", script, str(tmp_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout is not None
            assert process.stdout.readline().strip() == "locked"
            with pytest.raises(BusyError), c.gate.transaction(OperationToken(time.monotonic() + 0.02)):
                pytest.fail("other process reset must prevent admission")
            process.kill()
            process.wait(timeout=5)
            with c.gate.reset_window("opportunistic"):
                assert c.execute("select 1").get == 1
            fd = os.open(ws.locks / "checkpoint.lock", os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
            c.close()


def test_factory_configure_query_only_and_failed_init_remain_gated(tmp_path, monkeypatch):
    cfg = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(cfg) as ws, SQLiteRuntime(ws, cfg) as runtime:
        original = ManagedConnection.pragma
        observed = []

        def pragma(self, name, *args, **kwargs):
            if name in ("query_only", "journal_mode", "wal_autocheckpoint", "journal_size_limit"):
                assert self.gate.active
                observed.append(name)
            return original(self, name, *args, **kwargs)

        monkeypatch.setattr(ManagedConnection, "pragma", pragma)
        runtime.open_reader().close()
        assert {"query_only", "journal_mode", "wal_autocheckpoint", "journal_size_limit"} <= set(observed)
        failed = []

        def configure(connection):
            assert connection.gate.active
            failed.append(connection)
            raise OSError("injected configure failure")

        monkeypatch.setattr(runtime, "configure", configure)
        with pytest.raises(OSError, match="configure failure"):
            runtime.connect()
        assert failed[0].gate.closed
        with pytest.raises(apsw.ConnectionClosedError):
            failed[0].get_autocommit()


async def test_reset_rejection_captures_cached_shared_retry_delay(kb):
    await kb.status_sampler.close()
    await kb.job_runner.close()
    await kb.maintenance.close()
    cache = kb.workers.factory.status_cache
    previous_seq = cache.snapshot()["sample_seq"]
    assert isinstance(previous_seq, int)
    now = time.time()
    sample = WalState(
        phase="normal",
        sample_seq=previous_seq + 1,
        sample_at=now,
        allocated_bytes=0,
        log_frames=0,
        checkpointed_frames=0,
        page_size=4096,
        unbackfilled_bytes=0,
        next_attempt_not_before=now + 3,
    )
    assert sample.valid_sample(now)
    cache.update(sample)
    assert cache.snapshot()["sample_seq"] == sample.sample_seq
    token = OperationToken(time.monotonic() + 0.03)
    lock = os.open(kb.workspace.locks / "reset-intent.lock", os.O_RDWR)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises((BusyError, LimitError)):
            await kb.workers.read(lambda c, t: pytest.fail("reset admission failed"), token)
    finally:
        os.close(lock)
    assert 2500 < token.wal_retry_after_ms <= 3000
