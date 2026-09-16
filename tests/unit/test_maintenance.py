"""Checkpoint orchestration with isolated connection, file measurements and locks."""

import json
from contextlib import nullcontext, suppress
from types import SimpleNamespace
from unittest.mock import Mock

import apsw
import pytest

from justpen_knowledgebase_mcp.config import WorkspacePolicy
from justpen_knowledgebase_mcp.errors import (
    BusyError,
    ConfigurationError,
    LimitError,
    StorageIOError,
    UnsupportedLayoutError,
)
from justpen_knowledgebase_mcp.storage import maintenance
from justpen_knowledgebase_mcp.storage.maintenance import StatusCache, WalState

from .helpers import cursor, database


def component(monkeypatch):
    factory = Mock(config=SimpleNamespace(db_busy_timeout_ms=100), status_cache=StatusCache())
    factory.guard.policy.return_value = WorkspacePolicy()
    db = database()
    db.retired = False
    db.gate.closed = True
    db.gate.transaction.side_effect = lambda _token: nullcontext()
    db.gate.reset_window.side_effect = lambda _mode: nullcontext(SimpleNamespace(busy_timeout_ms=200))
    factory.open_maintenance.return_value = db
    monkeypatch.setattr(maintenance, "open_lock", Mock(return_value=9))
    monkeypatch.setattr(maintenance.fcntl, "flock", Mock())
    monkeypatch.setattr(maintenance.os, "close", Mock())
    monkeypatch.setattr(maintenance, "allocation", Mock(return_value=0))
    return maintenance.CheckpointMaintenance(factory), db


def test_start_cooldown_forward_clock_clamp_and_publication(monkeypatch):
    task, db = component(monkeypatch)
    monkeypatch.setattr(maintenance.time, "time", lambda: 100.0)
    for next_attempt, expected in [(None, True), (100.5, False), (200, False)]:
        db.execute.return_value.get = WalState(next_attempt_not_before=next_attempt).model_dump_json()
        result = task._start_attempt(db)
        assert (result is not None) == expected
        due = task.status()["next_attempt_not_before"]
        assert isinstance(due, float)
        assert due <= 101
    db.execute.side_effect = OSError("private path")
    with pytest.raises(OSError):
        task._publish(db, WalState())
    db.rollback_or_retire.assert_called_once()


@pytest.mark.parametrize(
    ("phase", "checkpoint", "result"),
    [("normal", (1, 1), "passive"), ("unknown", (1, 1), "restart"), ("unknown", (-1, -1), "invalid")],
)
def test_checkpoint_pressure_and_restart_publish_inside_scope(monkeypatch, phase, checkpoint, result):
    task, db = component(monkeypatch)
    db.wal_checkpoint.return_value = checkpoint
    db.pragma.return_value = 4096
    task._attempt(db, WalState(phase=phase), "timer")
    assert task.status()["last_attempt"] == result
    assert task.status()["phase"] == ("unknown" if result == "invalid" else "normal")
    assert task.status()["checkpoint_mode"] == ("PASSIVE" if result == "passive" else "RESTART")
    assert db.execute.call_args.args == ("COMMIT",)


def test_restart_busy_publishes_conservative_sample(monkeypatch):
    task, db = component(monkeypatch)
    db.wal_checkpoint.side_effect = [(100000, 0), apsw.BusyError()]
    db.pragma.return_value = 4096
    task._attempt(db, WalState(), "pressure")
    assert task.status()["last_attempt"] == "skipped_busy"
    assert task.status()["phase"] == "pressure"
    assert db.set_busy_timeout.call_args.args == (100,)


def test_run_once_leader_contention_failure_and_cleanup(monkeypatch):
    task, db = component(monkeypatch)
    flock = maintenance.fcntl.flock
    monkeypatch.setattr(task, "_start_attempt", Mock(return_value=WalState()))
    monkeypatch.setattr(task, "_attempt", Mock(side_effect=apsw.IOError("secret")))
    result = task.run_once("startup")
    assert result["last_attempt"] == "failed"
    assert result["phase"] == "unknown"
    assert isinstance(flock, Mock)
    assert flock.call_count == 2
    monkeypatch.setattr(maintenance.fcntl, "flock", Mock(side_effect=BlockingIOError()))
    assert task.run_once("timer")["last_attempt"] == "failed"
    task.close_owner()
    assert isinstance(task.factory, Mock)
    task.factory.close_connection.assert_called_once_with(db)
    assert task.connection is None


async def test_thread_lifecycle_and_pressure_trigger_priority(monkeypatch):
    task, _db = component(monkeypatch)
    calls = Mock()
    monkeypatch.setattr(task, "run_once", calls)
    task.request("pressure")
    task.request("low")
    assert task._trigger == "pressure"
    await task.start()
    await task.close()
    assert calls.call_args_list[0].args == ("startup",)


def test_allocation_and_invalid_persisted_state():
    workspace = Mock()
    for error, expected in [(FileNotFoundError(), 0), (OSError(), None)]:
        workspace.validate_native.side_effect = error
        assert maintenance.allocation(workspace) is expected
    assert WalState.read(database(cursor(value='{"extra":"invalid"}'))).phase == "unknown"


def test_runtime_failure_is_visible_and_permanent_fault_rejects_admission(monkeypatch, caplog):
    task, _db = component(monkeypatch)
    monkeypatch.setattr(task, "_open", Mock(side_effect=ConfigurationError("private path")))
    with pytest.raises(ConfigurationError):
        task.run_once("pressure")
    assert task.status()["maintenance_error"] == "CONFIGURATION"
    assert task.status()["last_attempt"] == "failed"
    with pytest.raises(ConfigurationError, match="maintenance unavailable"):
        task.factory.status_cache.check_health()
    assert "maintenance_failed" in caplog.text
    assert "private path" not in caplog.text


def test_transient_open_failure_is_visible_and_retried(monkeypatch, caplog):
    task, db = component(monkeypatch)
    monkeypatch.setattr(task, "_start_attempt", Mock(return_value=None))
    task.run_once("startup")
    monkeypatch.setattr(task, "_open", Mock(side_effect=StorageIOError("private path")))
    assert task.run_once("pressure")["maintenance_error"] == "IO_ERROR"
    task.factory.status_cache.check_health()
    assert "private path" not in caplog.text
    monkeypatch.setattr(task, "_open", Mock(return_value=db))
    task.close_owner()


@pytest.mark.parametrize("boundary", ["open", "lock"])
async def test_initial_owner_open_failure_is_fail_fast(monkeypatch, boundary):
    task, _db = component(monkeypatch)
    if boundary == "open":
        monkeypatch.setattr(task.factory, "open_maintenance", Mock(side_effect=OSError("private startup path")))
    else:
        monkeypatch.setattr(maintenance, "open_lock", Mock(side_effect=OSError("private startup lock")))
    try:
        with pytest.raises(StorageIOError, match="maintenance unavailable"):
            await task.start()
    finally:
        with pytest.raises(StorageIOError, match="maintenance unavailable"):
            await task.close()


@pytest.mark.parametrize("boundary", ["guard", "start_publication", "finish_publication"])
async def test_first_iteration_storage_failure_is_fail_fast(monkeypatch, boundary):
    task, db = component(monkeypatch)
    db.wal_checkpoint.return_value = (1, 1)
    db.pragma.return_value = 4096
    reached = []

    def execute(sql, bindings=None):
        if sql.startswith("UPDATE settings SET maintenance"):
            assert bindings is not None
            finished = json.loads(bindings[0])["attempt_finished_at"] is not None
            if boundary == ("finish_publication" if finished else "start_publication"):
                reached.append(boundary)
                raise StorageIOError("private startup publication")
        return cursor(value=WalState().model_dump_json())

    def guard(_connection):
        if boundary == "guard":
            reached.append(boundary)
            raise StorageIOError("private startup guard")

    db.execute.side_effect = execute
    monkeypatch.setattr(task.factory.guard, "check", guard)
    try:
        with pytest.raises(StorageIOError, match="maintenance unavailable"):
            await task.start()
        assert reached == [boundary]
        assert task.status()["maintenance_error"] == "IO_ERROR"
    finally:
        with suppress(StorageIOError):
            await task.close()


@pytest.mark.parametrize("error", [apsw.BusyError(), apsw.IOError(), OSError(), BusyError(), LimitError()])
async def test_historically_deferred_startup_attempt_still_initializes(monkeypatch, error):
    task, _db = component(monkeypatch)
    monkeypatch.setattr(task, "_start_attempt", Mock(side_effect=error))
    try:
        await task.start()
        assert task.status()["maintenance_alive"]
    finally:
        await task.close()


def test_local_assessment_and_failure_survive_same_sequence_reads(monkeypatch):
    monkeypatch.setattr(maintenance.time, "time", lambda: 100.0)
    cache = StatusCache()
    state = WalState(
        phase="normal",
        sample_seq=5,
        sample_at=100.0,
        allocated_bytes=0,
        log_frames=0,
        checkpointed_frames=0,
        page_size=4096,
        unbackfilled_bytes=0,
    )
    cache.update(state)
    cache.assessment()
    cache.update(state)
    assert cache.snapshot()["phase"] == "assessment_pending"
    cache.failed(StorageIOError("bounded"), permanent=False)
    cache.update(state)
    assert cache.snapshot()["last_attempt"] == "failed"
    cache.update(state.model_copy(update={"sample_seq": 6}))
    assert cache.snapshot()["phase"] == "normal"
    assert cache.snapshot()["maintenance_error"] is None


def test_cache_keeps_newer_attempt_metadata_and_lower_sequence_age(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(maintenance.time, "time", lambda: now[0])
    cache = StatusCache()
    state = WalState(sample_seq=5, attempt_started_at=80.0, attempt_finished_at=90.0)
    cache.update(state)
    now[0] = 101.0
    cache.update(state.model_copy(update={"sample_seq": 4, "attempt_started_at": 99.0}))
    assert cache.snapshot()["cache_age"] == 1
    newer = state.model_copy(update={"attempt_started_at": 101.0, "attempt_id": "newer"})
    cache.update(newer)
    cache.update(state)
    assert cache.snapshot()["attempt_id"] == "newer"


@pytest.mark.parametrize("error", [RuntimeError("private"), apsw.CorruptError("private"), apsw.NotADBError("private")])
def test_permanent_iteration_failure_stays_failed_after_newer_samples(monkeypatch, error):
    task, _db = component(monkeypatch)
    monkeypatch.setattr(task, "_open", Mock(side_effect=error))
    with pytest.raises(Exception, match="maintenance unavailable"):
        task.run_once("pressure")
    task.factory.status_cache.update(WalState(sample_seq=7))
    assert task.status()["maintenance_failed_permanently"]


def test_iteration_does_not_swallow_baseexception(monkeypatch):
    task, _db = component(monkeypatch)
    monkeypatch.setattr(task, "_open", Mock(side_effect=KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        task.run_once("pressure")
    assert task.status()["maintenance_error"] is None


def fresh_sample(seq=1, phase: maintenance.Phase = "pressure"):
    return WalState(
        phase=phase,
        sample_seq=seq,
        sample_at=maintenance.time.time(),
        allocated_bytes=4096,
        log_frames=1,
        checkpointed_frames=0,
        page_size=4096,
        unbackfilled_bytes=4096,
    )


@pytest.mark.parametrize("permanent", [False, True])
def test_fresh_pressure_clears_only_transient_health(monkeypatch, permanent):
    monkeypatch.setattr(maintenance.time, "time", lambda: 100.0)
    cache = StatusCache()
    cache.failed(StorageIOError("bounded"), permanent=permanent)
    cache.update(fresh_sample())
    snapshot = cache.snapshot()
    assert snapshot["maintenance_error"] == ("IO_ERROR" if permanent else None)
    assert snapshot["maintenance_failed_permanently"] is permanent
    assert snapshot["phase"] == ("unknown" if permanent else "pressure")


@pytest.mark.parametrize("invalid", [{"sample_at": 101.0}, {"unbackfilled_bytes": 0}, {"sample_seq": 0}])
def test_invalid_sample_never_clears_transient_health(monkeypatch, invalid):
    monkeypatch.setattr(maintenance.time, "time", lambda: 100.0)
    cache = StatusCache()
    cache.failed(StorageIOError("bounded"), permanent=False)
    cache.update(fresh_sample().model_copy(update=invalid))
    assert cache.snapshot()["maintenance_error"] == "IO_ERROR"


def test_known_layout_reason_survives_maintenance(monkeypatch):
    task, _db = component(monkeypatch)
    monkeypatch.setattr(task, "_open", Mock(side_effect=UnsupportedLayoutError("job ownership")))
    with pytest.raises(
        ConfigurationError, match="unsupported job ownership layout; offline workspace upgrade required"
    ):
        task.run_once("startup")
    with pytest.raises(ConfigurationError, match="offline workspace upgrade required"):
        task.factory.status_cache.check_health()


def test_failure_logs_rate_limit_and_recovery_requires_fresh_measurement(monkeypatch, caplog):
    caplog.set_level("INFO")
    now = [100.0]
    monkeypatch.setattr(maintenance.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(maintenance.time, "time", lambda: now[0])
    task, _db = component(monkeypatch)
    monkeypatch.setattr(task, "_run_once", lambda _trigger: task.status())
    task.run_once("startup")
    for _ in range(4):
        task._record_failure(StorageIOError("SECRET-MARKER"), permanent=False)
    assert caplog.text.count("maintenance_failed") == 1
    assert task._retry_not_before == 101.0
    task.run_once("timer")  # A no-op is not recovery.
    assert "maintenance_recovered" not in caplog.text
    now[0] = 130.0
    task._record_failure(StorageIOError("SECRET-MARKER"), permanent=False)
    assert caplog.text.count("maintenance_failed") == 2
    task._record_failure(BusyError("SECRET-MARKER"), permanent=False)
    assert caplog.text.count("maintenance_failed") == 3

    def sample_then_fail(_trigger):
        task.factory.status_cache.update(fresh_sample())
        task._record_failure(StorageIOError("SECRET-MARKER"), permanent=False)
        return task.status()

    monkeypatch.setattr(task, "_run_once", sample_then_fail)
    task.run_once("timer")
    assert "maintenance_recovered" not in caplog.text

    monkeypatch.setattr(task, "_run_once", maintenance.CheckpointMaintenance._run_once.__get__(task))
    monkeypatch.setattr(task, "_start_attempt", lambda _db: fresh_sample())
    monkeypatch.setattr(task, "_attempt", lambda *_args: task.factory.status_cache.update(fresh_sample(2)))
    task.run_once("timer")
    task.run_once("timer")
    assert caplog.text.count("maintenance_recovered") == 1
    assert "suppressed=3" in caplog.text
    assert "SECRET-MARKER" not in caplog.text


def test_old_measurement_cannot_recover_even_with_newer_sequence(monkeypatch):
    monkeypatch.setattr(maintenance.time, "time", lambda: 100.0)
    cache = StatusCache()
    cache.failed(StorageIOError("bounded"), permanent=False)
    cache.update(fresh_sample().model_copy(update={"sample_at": 90.0}))
    assert cache.snapshot()["maintenance_error"] == "IO_ERROR"


def test_nonleader_cannot_report_recovery_from_another_owner_sample(monkeypatch, caplog):
    caplog.set_level("INFO")
    task, _db = component(monkeypatch)
    task._record_failure(StorageIOError("bounded"), permanent=False)
    task.factory.status_cache.update(fresh_sample().model_copy(update={"sample_at": maintenance.time.time()}))
    monkeypatch.setattr(maintenance.fcntl, "flock", Mock(side_effect=BlockingIOError()))
    task.run_once("timer")
    assert "maintenance_recovered" not in caplog.text


def test_health_rejections_do_not_accumulate_cached_exception_tracebacks():
    cache = StatusCache()
    error = UnsupportedLayoutError("supporting index")
    cache.failed(error, permanent=True)
    for _ in range(3):
        with pytest.raises(ConfigurationError, match="offline workspace upgrade required"):
            cache.check_health()
    assert error.__traceback__ is None


def test_invalid_local_cycle_cannot_report_recovery_after_peer_sample(monkeypatch, caplog):
    caplog.set_level("INFO")
    task, _db = component(monkeypatch)
    task._record_failure(StorageIOError("bounded"), permanent=False)
    task.factory.status_cache.update(fresh_sample())
    monkeypatch.setattr(task, "_start_attempt", lambda _db: fresh_sample())
    monkeypatch.setattr(task, "_attempt", lambda *_args: task.factory.status_cache.update(WalState(sample_seq=2)))
    task.run_once("timer")
    assert "maintenance_recovered" not in caplog.text
