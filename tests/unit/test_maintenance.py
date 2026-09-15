"""Checkpoint orchestration with isolated connection, file measurements and locks."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import apsw
import pytest

from justpen_knowledgebase_mcp.config import WorkspacePolicy
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
