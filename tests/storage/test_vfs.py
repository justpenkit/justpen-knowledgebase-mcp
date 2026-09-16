"""Native spills and fail-closed temporary storage."""

import os

import apsw
import pytest

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.storage.connection import SQLiteRuntime
from justpen_knowledgebase_mcp.workspace import WorkspacePaths

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("failure", ["deleted", "permissions"])
def test_native_spill_and_deleted_tmp_fails_closed(tmp_path, failure):

    cfg = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(cfg) as ws, SQLiteRuntime(ws, cfg) as runtime:
        c = runtime.connect()
        try:
            c.pragma("cache_size", -64)
            c.execute("create table data(x)")
            c.execute(
                "with recursive n(x) as (values(1) union all select x+1 from n where x<30000) insert into data select randomblob(2048) from n"
            )
            assert sum(1 for _ in c.execute("select x from data order by x")) == 30000
            assert runtime.vfs.temp_open_count > 0
            assert not list(ws.tmp.iterdir())
            if failure == "deleted":
                ws.tmp.rmdir()
            else:
                ws.tmp.chmod(0o500)
            try:
                with pytest.raises(apsw.IOError, match="IO_ERROR"):
                    list(c.execute("select x from data order by x"))
            finally:
                if failure == "permissions":
                    ws.tmp.chmod(0o700)
        finally:
            c.close()


def test_vfs_denies_named_escape_symlink_and_hardlink(tmp_path):

    cfg = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(cfg) as ws, SQLiteRuntime(ws, cfg) as runtime:
        for operation in [
            lambda: runtime.vfs.xFullPathname("/outside/db"),
            lambda: runtime.vfs.xDelete("/outside/db", syncdir=False),
            lambda: runtime.vfs.xOpen("/outside/db", [apsw.SQLITE_OPEN_READWRITE | apsw.SQLITE_OPEN_CREATE, 0]),
        ]:
            with pytest.raises(apsw.IOError):
                operation()
        (ws.tmp / "alias").symlink_to(ws.db)
        with pytest.raises(apsw.IOError):
            runtime.vfs.xOpen(str(ws.tmp / "alias"), [apsw.SQLITE_OPEN_READWRITE, 0])


@pytest.mark.parametrize("sidecar", ["-wal", "-shm", "-journal"])
def test_existing_native_sidecar_hardlink_is_rejected(tmp_path, sidecar):
    cfg = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(cfg) as ws, SQLiteRuntime(ws, cfg) as runtime:
        source = tmp_path / "source"
        source.write_bytes(b"alias")
        os.link(source, str(ws.db) + sidecar)
        with pytest.raises(apsw.IOError):
            runtime.connect()
        assert source.read_bytes() == b"alias"
