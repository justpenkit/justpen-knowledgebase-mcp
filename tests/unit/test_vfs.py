"""Native VFS naming decisions without registering or opening a native filesystem."""

from pathlib import Path
from unittest.mock import Mock

import apsw
import pytest

from justpen_knowledgebase_mcp.errors import PathDeniedError
from justpen_knowledgebase_mcp.storage.vfs import WorkspaceVFS


def wrapper():
    workspace = Mock(db=Path("/workspace/db/graph.sqlite3"), tmp=Path("/workspace/tmp"))
    workspace.absolute.side_effect = Path
    workspace.validate_native.side_effect = lambda path: path
    value = Mock(workspace=workspace, temp_open_count=0)
    value._validate.side_effect = lambda name: WorkspaceVFS._validate(value, name)
    value._allowed.side_effect = lambda name: WorkspaceVFS._allowed(value, name)
    return value


def test_vfs_only_accepts_database_sidecars_and_owned_temp():
    value = wrapper()
    for path in [str(value.workspace.db) + suffix for suffix in ("", "-wal", "-shm", "-journal")]:
        assert WorkspaceVFS.xFullPathname(value, path) == path
    assert WorkspaceVFS._validate(value, "/workspace/tmp/spill") == "/workspace/tmp/spill"
    with pytest.raises(PathDeniedError):
        WorkspaceVFS._validate(value, "/workspace/private")
    with pytest.raises(apsw.IOError, match="SQLite path unavailable"):
        WorkspaceVFS._allowed(value, "/outside/private")


def test_native_open_validates_sidecars_and_forces_no_follow(monkeypatch):
    value = wrapper()
    opened = Mock()
    monkeypatch.setattr(apsw, "VFSFile", opened)
    flags = [apsw.SQLITE_OPEN_READWRITE, 0]
    assert WorkspaceVFS.xOpen(value, str(value.workspace.db), flags) is opened.return_value
    assert flags[0] & apsw.SQLITE_OPEN_NOFOLLOW
    assert len(value._allowed.call_args_list) == 4
    WorkspaceVFS.xOpen(value, None, flags)
    assert value.temp_open_count == 1
    assert Path(opened.call_args.args[1]).parent == value.workspace.tmp
    opened.side_effect = apsw.CantOpenError("private native path")
    with pytest.raises(apsw.IOError, match="native SQLite open failed"):
        WorkspaceVFS.xOpen(value, str(value.workspace.db), flags)
