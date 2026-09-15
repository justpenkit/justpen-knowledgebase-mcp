"""Real WAL, JSON, FTS and concurrent connection configuration."""

import json
import stat
import sys

import apsw
import pytest

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import ConfigurationError
from justpen_knowledgebase_mcp.storage.connection import SQLiteRuntime
from justpen_knowledgebase_mcp.workspace import WorkspacePaths

pytestmark = pytest.mark.integration


def test_real_wal_full_fts_checkpoint_and_reopen(tmp_path):

    cfg = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(cfg) as ws, SQLiteRuntime(ws, cfg) as runtime:
        first = runtime.connect()
        second = runtime.connect()
        try:
            assert first.pragma("journal_mode") == "wal"
            assert first.pragma("foreign_keys") == 1
            assert first.pragma("synchronous") == 2
            assert first.pragma("busy_timeout") == 5000
            if sys.platform == "darwin":
                assert first.pragma("fullfsync") == 1
                assert first.pragma("checkpoint_fullfsync") == 1
            first.execute("create virtual table search using fts5(body)")
            first.execute("insert into search values ('https://admin.example.com/api')")
            assert second.execute("select count(*) from search where search match ?", ('"admin example com"',)).get == 1
            second.execute("create table objects(body check(json_valid(body)))")
            second.execute("insert into objects values (?)", ('{"ok":true}',))
            assert first.execute("select json_extract(body,'$.ok') from objects").get == 1
            assert ws.db.with_name(ws.db.name + "-wal").is_file()
            assert ws.db.with_name(ws.db.name + "-shm").is_file()
            assert first.wal_checkpoint(mode=apsw.SQLITE_CHECKPOINT_TRUNCATE) == (0, 0)
        finally:
            second.close()
            first.close()
        reopened = runtime.connect()
        assert reopened.execute("select count(*) from objects").get == 1
        reopened.close()


def test_same_db_different_evidence_is_configuration_error(tmp_path):

    cfg = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(cfg) as ws, SQLiteRuntime(ws, cfg) as runtime:
        runtime.connect().close()
    changed = ServerConfig(workspace_dir=tmp_path, evidence_dir=tmp_path / "other")
    with (
        WorkspacePaths(changed) as ws,
        SQLiteRuntime(ws, changed) as runtime,
        pytest.raises(ConfigurationError, match="contract or managed paths"),
    ):
        runtime.connect()


def test_native_db_wal_shm_permissions_are_private(tmp_path):
    cfg = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(cfg) as ws, SQLiteRuntime(ws, cfg) as runtime:
        c = runtime.connect()
        try:
            for path in [ws.db, ws.db.with_name(ws.db.name + "-wal"), ws.db.with_name(ws.db.name + "-shm")]:
                assert stat.S_IMODE(path.stat().st_mode) == 0o600
        finally:
            c.close()


def test_second_writer_obeys_busy_timeout_then_recovers(tmp_path):
    cfg = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(cfg) as ws, SQLiteRuntime(ws, cfg) as runtime:
        first = runtime.connect()
        second = runtime.connect()
        # Exercise the product lock wait independently of native startup I/O.
        second.set_busy_timeout(1)
        try:
            first.execute("create table writes(value integer)")
            first.execute("begin immediate")
            first.execute("insert into writes values (1)")
            with pytest.raises(apsw.BusyError):
                second.execute("insert into writes values (2)")
            first.execute("commit")
            second.execute("insert into writes values (2)")
            assert list(first.execute("select value from writes order by value")) == [(1,), (2,)]
        finally:
            second.close()
            first.close()


def test_policy_is_persisted_and_applied_on_every_connection(tmp_path):

    cfg = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(cfg) as ws, SQLiteRuntime(ws, cfg) as runtime:
        connection = runtime.connect()
        try:
            policy = json.loads(connection.execute("select policy from settings").get)
            assert policy.get("wal_high_bytes") == 268435456
            assert connection.pragma("journal_size_limit") == 67108864
            assert connection.pragma("wal_autocheckpoint") == 1000
            assert json.loads(connection.execute("select terminal_job_counts from settings").get) == {
                "completed": 0,
                "failed_cancelled": 0,
            }
        finally:
            connection.close()
        reopened = runtime.open_writer()
        try:
            assert json.loads(reopened.execute("select policy from settings").get) == policy
            assert reopened.pragma("journal_size_limit") == 67108864
        finally:
            reopened.close()
