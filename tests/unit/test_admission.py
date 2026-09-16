"""Admission lock ordering with kernel flock/open/close calls isolated."""

import fcntl
from pathlib import Path
from unittest.mock import Mock

import pytest

from justpen_knowledgebase_mcp.errors import WalBusyError
from justpen_knowledgebase_mcp.storage import admission
from justpen_knowledgebase_mcp.storage.worker import OperationToken


@pytest.fixture
def locks(monkeypatch):
    monkeypatch.setattr(admission, "open_lock", Mock(side_effect=[10, 11]))
    flock, close = Mock(), Mock()
    monkeypatch.setattr(admission.fcntl, "flock", flock)
    monkeypatch.setattr(admission.os, "close", close)
    return flock, close


def test_transaction_scope_lock_order_cleanup_and_reentrancy(locks):
    flock, close = locks
    gate = admission.DbAdmissionGate(Mock())
    token = OperationToken(float("inf"))
    with gate.transaction(token):
        assert gate.active
        assert gate.active_token is token
        with pytest.raises(RuntimeError, match="active"):
            gate.close()
        with pytest.raises(RuntimeError, match="already active"), gate.transaction(token):
            pass
    assert not gate.active
    assert gate.active_token is None
    assert [call.args for call in flock.call_args_list] == [
        (10, fcntl.LOCK_SH | fcntl.LOCK_NB),
        (11, fcntl.LOCK_SH | fcntl.LOCK_NB),
        (10, fcntl.LOCK_UN),
        (11, fcntl.LOCK_UN),
    ]
    gate.close()
    gate.close()
    assert [call.args for call in close.call_args_list] == [(11,), (10,)]
    with pytest.raises(RuntimeError, match="closed"):
        gate.check_owner()


def test_partial_acquisition_releases_only_acquired_lock(locks, monkeypatch):
    flock, _close = locks
    gate = admission.DbAdmissionGate(Mock())
    flock.side_effect = [None, BlockingIOError(), None]
    monkeypatch.setattr(admission.time, "monotonic", lambda: 2)
    with pytest.raises(WalBusyError), gate.reset_window("opportunistic"):
        pass
    assert not gate.active
    assert flock.call_args.args == (10, fcntl.LOCK_UN)


@pytest.mark.parametrize("mode", ["opportunistic", "pressure"])
def test_reset_window_holds_exclusive_locks_through_body(locks, mode):
    flock, _close = locks
    gate = admission.DbAdmissionGate(Mock())
    with gate.reset_window(mode) as window:
        assert gate.active
        assert 0 <= window.busy_timeout_ms <= 200
        assert flock.call_count == 2
    assert flock.call_count == 4
    with pytest.raises(ValueError), gate.reset_window(Mock()):
        pass


def test_open_lock_validates_existing_and_closes_parent(monkeypatch):
    workspace = Mock(locks=Path("/workspace/locks"))
    workspace.validate_native.side_effect = lambda path: path
    workspace.open_directory.return_value = 9
    opened = Mock(side_effect=[FileExistsError(), 10])
    closed = Mock()
    monkeypatch.setattr(admission.os, "open", opened)
    monkeypatch.setattr(admission.os, "close", closed)
    assert admission.open_lock(workspace, "lock") == 10
    assert workspace.validate_native.call_count == 2
    closed.assert_called_once_with(9)
