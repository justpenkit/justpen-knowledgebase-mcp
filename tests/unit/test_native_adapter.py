"""Native adapter configuration/retirement contracts with APSW and OS isolated."""

from contextlib import nullcontext
from unittest.mock import Mock

import apsw
import pytest

from justpen_knowledgebase_mcp.config import WorkspacePolicy
from justpen_knowledgebase_mcp.errors import ConfigurationError, LimitError, StorageIOError, WalBusyError
from justpen_knowledgebase_mcp.storage import connection, filesystem
from justpen_knowledgebase_mcp.storage.maintenance import WalState


def runtime(monkeypatch):
    monkeypatch.setattr(connection, "_bootstrap_mutex", Mock(return_value=Mock()))
    monkeypatch.setattr(connection, "WorkspaceVFS", Mock())
    monkeypatch.setattr(connection, "SchemaGuard", Mock())
    return connection.SQLiteRuntime(Mock(), Mock(db_busy_timeout_ms=100))


def test_runtime_checks_native_version_and_platform(monkeypatch):
    monkeypatch.setattr(connection.apsw, "sqlitelibversion", lambda: "3.50.0")
    with pytest.raises(ConfigurationError):
        runtime(monkeypatch)
    monkeypatch.setattr(connection.apsw, "sqlitelibversion", lambda: "3.51.3")
    monkeypatch.setattr(connection.sys, "platform", "unsupported")
    with pytest.raises(ConfigurationError):
        runtime(monkeypatch)


def test_configure_enforces_durability_and_native_features(monkeypatch):
    factory = runtime(monkeypatch)
    db = Mock(startup_deadline=float("inf"))
    monkeypatch.setattr(connection, "_remaining_wait_ms", lambda _deadline: 100)
    monkeypatch.setattr(connection.WalState, "read", Mock(return_value=WalState()))
    db.pragma.return_value = "wal"
    db.execute.return_value.get = (1, 1)
    assert isinstance(factory.guard, Mock)
    factory.guard.policy.return_value = WorkspacePolicy()
    factory.configure(db)
    pragmas = [call.args for call in db.pragma.call_args_list]
    assert ("foreign_keys", 1) in pragmas
    assert ("synchronous", "full") in pragmas
    assert ("temp_store", "file") in pragmas
    db.enable_load_extension.assert_called_once_with(enable=False)
    factory.guard.initialize.assert_called_once_with(db)
    db.execute.return_value.get = (1, 0)
    with pytest.raises(ConfigurationError, match="JSON and FTS5"):
        factory.configure(db)
    db.pragma.return_value = "delete"
    with pytest.raises(ConfigurationError, match="WAL unavailable"):
        factory.configure(db)
    with factory:
        pass
    assert isinstance(factory.vfs, Mock)
    factory.vfs.unregister.assert_called_once()


def test_bootstrap_uses_one_budget_and_releases_mutex(monkeypatch):
    factory = runtime(monkeypatch)
    with factory.bootstrap() as token:
        token.check()
    assert isinstance(factory._bootstrap_mutex, Mock)
    factory._bootstrap_mutex.lock.release.assert_called_once()
    factory._bootstrap_mutex.lock.acquire.return_value = False
    with pytest.raises(LimitError), factory.bootstrap():
        pass
    monkeypatch.setattr(connection.time, "monotonic", lambda: 10)
    assert connection._remaining_wait_ms(10.1) >= 99
    with pytest.raises(LimitError):
        connection._remaining_wait_ms(10)


@pytest.mark.parametrize(
    ("allocation", "age", "phase", "blocked", "wake"),
    [
        (0, 0, "normal", False, None),
        (0, 40, "normal", False, "stale"),
        (None, 0, "normal", True, "pressure"),
        (0, 0, "unknown", True, "pressure"),
    ],
)
def test_product_admission_uses_physical_and_shared_evidence(monkeypatch, allocation, age, phase, blocked, wake):
    factory = runtime(monkeypatch)
    assert isinstance(factory.guard, Mock)
    factory.guard.policy.return_value = WorkspacePolicy()
    factory.wake_maintenance = Mock()
    monkeypatch.setattr(connection, "allocation", lambda _workspace: allocation)
    monkeypatch.setattr(connection.time, "time", lambda: 100)
    state = WalState(
        phase=phase,
        sample_seq=1,
        sample_at=float(100 - age),
        allocated_bytes=0,
        log_frames=0,
        checkpointed_frames=0,
        page_size=4096,
        unbackfilled_bytes=0,
    )
    monkeypatch.setattr(connection.WalState, "read", Mock(return_value=state))
    if blocked:
        with pytest.raises(WalBusyError):
            factory.check_product(Mock())
    else:
        factory.check_product(Mock())
    if wake:
        factory.wake_maintenance.assert_called_once_with(wake)
    else:
        factory.wake_maintenance.assert_not_called()
    factory.committed(Mock(policy=WorkspacePolicy()))


def test_enable_wal_and_close_retries_are_bounded_by_owner(monkeypatch):
    factory = runtime(monkeypatch)
    monkeypatch.setattr(connection.time, "sleep", Mock())
    db = Mock(startup_deadline=float("inf"))
    monkeypatch.setattr(connection.time, "monotonic", lambda: 1)
    db.startup_deadline = 2
    db.pragma.side_effect = [apsw.BusyError(), "wal"]
    assert factory._enable_wal(db) == "wal"
    db.close.side_effect = [LimitError("busy"), None]
    factory.close_connection(db)
    assert db.close.call_count == 2


@pytest.mark.parametrize("autocommit", [[True, True], [False, True], [False, False]])
def test_failed_rollback_retires_before_releasing_gate(autocommit):
    db = Mock()
    db.gate.active = True
    db.get_autocommit.side_effect = autocommit
    error = connection.ManagedConnection.rollback_or_retire(db)
    if autocommit[-1]:
        assert error is None
        db._finish_native_close.assert_not_called()
    else:
        assert isinstance(error, StorageIOError)
        db._finish_native_close.assert_called_once_with(force=True)
        assert db._retired is True


def test_native_close_retry_retains_first_failure(monkeypatch):
    monkeypatch.setattr(connection.time, "sleep", Mock())
    db = Mock()
    original = OSError("native")
    db.close_native.side_effect = [original, None]
    db.get_autocommit.side_effect = [True, apsw.ConnectionClosedError()]
    assert connection.ManagedConnection._finish_native_close(db, force=False) is original
    assert [call.kwargs for call in db.close_native.call_args_list] == [{"force": False}, {"force": True}]
    db.gate.closed = False
    db.retired = False
    db.close_timeout_ms = 100
    db.gate.transaction.side_effect = lambda _token: nullcontext()
    db._finish_native_close.return_value = None
    connection.ManagedConnection.close(db)
    db.gate.close.assert_called_once()


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_locality_probe_routes_native_function_and_fails_closed(monkeypatch, platform):

    monkeypatch.setattr(filesystem.sys, "platform", platform)
    monkeypatch.setattr(filesystem.ctypes, "sizeof", Mock(return_value=8))
    libc = Mock()
    monkeypatch.setattr(filesystem.ctypes, "CDLL", Mock(return_value=libc))
    probe = libc.fstatfs if platform == "linux" else libc.fstatfs64
    probe.return_value = -1
    with pytest.raises(ConfigurationError, match="locality unavailable"):
        filesystem.validate_local_directory(9)
    assert probe.call_args.args[0] == 9
    probe.return_value = 0
    # Zero-filled result represents unknown/non-local; never infer safety from syscall success.
    with pytest.raises(ConfigurationError, match="non-local"):
        filesystem.validate_local_directory(9)
    filesystem.require_local(platform, 0xEF53, 0x1000)


def test_retired_native_owner_only_releases_gate_and_inactive_cleanup_rejected():
    db = Mock(retired=True)
    db.gate.closed = False
    connection.ManagedConnection.close(db)
    db.gate.close.assert_called_once()
    db._finish_native_close.assert_not_called()
    db.gate.active = False
    with pytest.raises(RuntimeError, match="gate scope"):
        connection.ManagedConnection.rollback_or_retire(db)
