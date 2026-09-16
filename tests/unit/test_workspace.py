"""Pinned workspace boundary decisions with OS descriptors and stat calls isolated."""

import errno
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from justpen_knowledgebase_mcp import workspace
from justpen_knowledgebase_mcp.errors import PathDeniedError, StorageIOError


@pytest.fixture
def paths(monkeypatch):
    # A preinitialized pinned owner isolates path operations from startup/native probing.
    value = object.__new__(workspace.WorkspacePaths)
    value.root = Path("/workspace")
    value.configured_root = Path("/alias")
    value.root_fd = 3
    value.data = Path("/workspace/data")
    value.db = value.data / "graph.sqlite3"
    value.evidence = value.data / "evidence"
    value.tmp = value.data / "tmp"
    value.locks = value.data / "locks"
    value._fds = {value.tmp: 4, value.db.parent: 5}
    monkeypatch.setattr(workspace.os, "open", Mock(return_value=7))
    monkeypatch.setattr(workspace.os, "dup", Mock(return_value=6))
    monkeypatch.setattr(workspace.os, "close", Mock())
    monkeypatch.setattr(workspace.os, "mkdir", Mock())
    monkeypatch.setattr(workspace.os, "fsync", Mock())
    monkeypatch.setattr(workspace.os, "unlink", Mock())
    monkeypatch.setattr(workspace.os, "rename", Mock())
    metadata = SimpleNamespace(
        st_dev=1, st_ino=2, st_mode=stat.S_IFREG, st_nlink=1, st_size=3, st_mtime_ns=4, st_ctime_ns=5
    )
    monkeypatch.setattr(workspace.os, "fstat", Mock(return_value=metadata))
    monkeypatch.setattr(workspace.os, "stat", Mock(return_value=metadata))
    return value


def test_relative_and_managed_boundaries(paths):
    assert paths.absolute("/alias/input") == Path("/workspace/input")
    assert paths.relative("/workspace/input") == Path("input")
    for path in ("../input", "/outside/input"):
        with pytest.raises(PathDeniedError):
            paths.relative(path)
    assert paths._is_managed(paths.evidence / "blob")
    assert paths._is_managed(Path(str(paths.db) + "-wal"))
    assert not paths._is_managed(paths.root / "input")


def test_traversal_no_follow_flags_and_parent_cleanup(paths, monkeypatch):
    opened = Mock(side_effect=[8, 9])
    monkeypatch.setattr(workspace.os, "open", opened)
    assert paths.open_directory("one/two", create=True) == 9
    assert [call.kwargs["dir_fd"] for call in opened.call_args_list] == [6, 8]
    assert all(call.args[1] & workspace.os.O_NOFOLLOW for call in opened.call_args_list)
    opened.side_effect = OSError(errno.ELOOP, "symlink")
    with pytest.raises(PathDeniedError, match="SYMLINK_COMPONENT"):
        paths.open_directory("one")


def test_native_rejects_alias_and_changed_pinned_directory(paths, monkeypatch):
    monkeypatch.setattr(paths, "open_directory", Mock(return_value=7))
    assert paths.validate_native(paths.db) == paths.db
    monkeypatch.setattr(
        workspace.os,
        "fstat",
        Mock(side_effect=[SimpleNamespace(st_dev=1, st_ino=2), SimpleNamespace(st_dev=1, st_ino=9)]),
    )
    with pytest.raises(StorageIOError, match="DIRECTORY_CHANGED"):
        paths.validate_native(paths.db)
    monkeypatch.setattr(workspace.os, "fstat", Mock(return_value=SimpleNamespace(st_dev=1, st_ino=2)))
    monkeypatch.setattr(workspace.os, "stat", Mock(return_value=SimpleNamespace(st_mode=stat.S_IFREG, st_nlink=2)))
    with pytest.raises(PathDeniedError, match="UNSAFE_MANAGED"):
        paths.validate_native(paths.db)
    monkeypatch.setattr(workspace.os, "stat", Mock(side_effect=FileNotFoundError()))
    assert paths.validate_native(paths.db) == paths.db


def test_import_detects_changed_bytes_and_rejects_managed_source(paths, monkeypatch):
    monkeypatch.setattr(paths, "open_directory", Mock(return_value=6))
    with paths.open_import("input") as fd:
        assert fd == 7
    with pytest.raises(PathDeniedError, match="MANAGED_SOURCE"), paths.open_import(paths.db):
        pass
    first = SimpleNamespace(st_mode=stat.S_IFREG, st_nlink=1, st_size=3, st_mtime_ns=4, st_ctime_ns=5)
    second = SimpleNamespace(st_size=4, st_mtime_ns=4, st_ctime_ns=5)
    monkeypatch.setattr(workspace.os, "fstat", Mock(side_effect=[first, second]))
    with pytest.raises(StorageIOError, match="SOURCE_CHANGED"), paths.open_import("input"):
        pass


def test_managed_publication_syncs_before_rename_and_closes_owners(paths, monkeypatch):
    monkeypatch.setattr(paths, "open_directory", Mock(return_value=6))
    monkeypatch.setattr(paths, "validate_native", Mock())
    calls = []
    monkeypatch.setattr(workspace.os, "fsync", Mock(side_effect=lambda fd: calls.append(("sync", fd))))
    monkeypatch.setattr(workspace.os, "rename", Mock(side_effect=lambda *_args, **_kwargs: calls.append(("rename",))))
    paths.publish("owned", "aa/bb/hash")
    assert calls == [("sync", 7), ("rename",), ("sync", 6)]
    calls.clear()
    paths.publish("owned", "aa/bb/hash", durable_stage=True)
    assert calls == [("rename",), ("sync", 6)]
    for source, destination in [("../bad", "hash"), ("owned", "/absolute"), ("owned", "../hash")]:
        with pytest.raises(PathDeniedError):
            paths.publish(source, destination)
    assert paths.create_managed_file(paths.tmp / "new") == 7
    with paths.open_managed_file(paths.tmp / "new") as fd:
        assert fd == 7
    paths.unlink_managed_file(paths.tmp / "new")
    with paths.stage() as (name, fd):
        assert len(name) == 32
        assert fd == 7
    paths.close()
    paths.close()
    assert paths.root_fd == -1
    assert not paths._fds


@pytest.mark.parametrize("failure", [None, "root_changed", "device_mismatch", "locality"])
def test_workspace_startup_checks_pinned_identity_devices_and_unwinds(monkeypatch, failure):
    config = Mock(
        workspace_dir=Path("/configured"),
        data_dir=Path("data"),
        db_path=None,
        evidence_dir=None,
        tmp_dir=None,
        lock_dir=None,
    )
    monkeypatch.setattr(Path, "resolve", Mock(return_value=Path("/workspace")))
    monkeypatch.setattr(Path, "stat", Mock(return_value=SimpleNamespace(st_dev=1, st_ino=2)))
    monkeypatch.setattr(workspace.os, "open", Mock(side_effect=[3, 9]))
    close = Mock()
    monkeypatch.setattr(workspace.os, "close", close)

    def metadata(fd):
        return SimpleNamespace(
            st_dev=2 if failure == "device_mismatch" and fd == 6 else 1, st_ino=9 if failure == "root_changed" else 2
        )

    monkeypatch.setattr(workspace.os, "fstat", Mock(side_effect=metadata))
    monkeypatch.setattr(workspace.WorkspacePaths, "open_directory", Mock(side_effect=[4, 5, 6, 7]))
    monkeypatch.setattr(workspace.WorkspacePaths, "validate_native", Mock())
    monkeypatch.setattr(
        workspace,
        "validate_local_directory",
        Mock(side_effect=workspace.ConfigurationError("non-local") if failure == "locality" else None),
    )
    if failure:
        with pytest.raises(workspace.ConfigurationError) as captured:
            workspace.WorkspacePaths(config)
        expected = {"root_changed": [(3,)], "device_mismatch": [(4,), (5,), (6,), (7,), (3,)], "locality": [(4,), (3,)]}
        assert [call.args for call in close.call_args_list] == expected[failure]
        if failure == "root_changed":
            assert str(captured.value) == "CONFIGURATION: ROOT_CHANGED"
    else:
        with workspace.WorkspacePaths(config) as value:
            assert value.db == Path("/workspace/data/graph.sqlite3")
            assert value.managed_fd(value.tmp) == 6
        assert value.root_fd == -1
        assert [call.args for call in close.call_args_list] == [(9,), (4,), (5,), (6,), (7,), (3,)]


@pytest.mark.parametrize(
    "failure", [None, "missing_child", "missing_leaf", "unsafe_leaf", "changed_root", "sync", "access"]
)
def test_orphan_absence_requires_pinned_root_and_durable_safe_traversal(paths, monkeypatch, failure):
    paths._fds[paths.evidence] = 8
    sync, unlink, close = Mock(), Mock(), Mock()
    monkeypatch.setattr(workspace.os, "fsync", sync)
    monkeypatch.setattr(workspace.os, "unlink", unlink)
    monkeypatch.setattr(workspace.os, "close", close)
    monkeypatch.setattr(paths, "open_directory", Mock(return_value=6))
    opened = Mock(return_value=7)
    monkeypatch.setattr(paths, "_directory", opened)
    if failure == "missing_child":
        opened.side_effect = FileNotFoundError()
    elif failure == "missing_leaf":
        monkeypatch.setattr(workspace.os, "stat", Mock(side_effect=FileNotFoundError()))
    elif failure == "unsafe_leaf":
        monkeypatch.setattr(workspace.os, "stat", Mock(return_value=SimpleNamespace(st_mode=stat.S_IFREG, st_nlink=2)))
    elif failure == "changed_root":
        monkeypatch.setattr(
            workspace.os,
            "fstat",
            Mock(side_effect=[SimpleNamespace(st_dev=1, st_ino=2), SimpleNamespace(st_dev=1, st_ino=9)]),
        )
    elif failure == "sync":
        sync.side_effect = OSError(errno.EIO, "sync")
    elif failure == "access":
        opened.side_effect = PermissionError(errno.EACCES, "access")
    if failure in {"unsafe_leaf", "changed_root", "sync", "access"}:
        with pytest.raises((PathDeniedError, StorageIOError, OSError)):
            paths.unlink_orphan_evidence("aa/bb/digest")
    else:
        paths.unlink_orphan_evidence("aa/bb/digest")
        sync.assert_called_once()
    if failure != "sync" and failure is not None:
        unlink.assert_not_called()
    assert close.call_args is not None
    assert close.call_args.args == ((6,) if failure in {"missing_child", "changed_root", "access"} else (7,))


@pytest.mark.parametrize("name", ["/aa/bb/hash", "aa/../hash", "hash"])
def test_orphan_unlink_rejects_invalid_relative_names(paths, name):
    with pytest.raises(PathDeniedError, match="INVALID_EVIDENCE_PATH"):
        paths.unlink_orphan_evidence(name)
