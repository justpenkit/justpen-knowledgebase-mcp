"""Real SQLite schema compatibility and atomic initialization."""

import concurrent.futures
from contextlib import closing

import apsw
import pytest

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import ConfigurationError
from justpen_knowledgebase_mcp.storage import schema
from justpen_knowledgebase_mcp.storage.connection import SQLiteRuntime
from justpen_knowledgebase_mcp.workspace import WorkspacePaths

pytestmark = pytest.mark.integration


def test_schema_initialized_and_reopened(tmp_path):
    config = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(config) as workspace, SQLiteRuntime(workspace, config) as runtime:
        identity = None
        with closing(runtime.connect()) as connection:
            assert connection.execute("select schema_version from settings").get == 1
            assert connection.execute("select count(*) from nodes").get == 0
            identity = connection.execute("select workspace_id from settings").get
        with closing(runtime.connect()) as connection:
            assert connection.execute("select workspace_id from settings").get == identity


def test_newer_schema_is_rejected(tmp_path):
    config = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(config) as workspace, SQLiteRuntime(workspace, config) as runtime:
        connection = runtime.connect()
        connection.execute("update settings set schema_version=2")
        connection.close()
        with pytest.raises(ConfigurationError):
            runtime.connect()


def test_unique_identity_and_foreign_keys(tmp_path):
    config = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(config) as workspace, SQLiteRuntime(workspace, config) as runtime:
        connection = runtime.connect()
        try:
            connection.execute("insert into nodes(uuid,type,key,properties) values ('one','ip','a','{}')")
            with pytest.raises(apsw.ConstraintError):
                connection.execute("insert into nodes(uuid,type,key,properties) values ('two','ip','a','{}')")
            with pytest.raises(apsw.ConstraintError):
                connection.execute(
                    "insert into relations(uuid,source_id,type,target_id,key,properties) values ('r',1,'resolves_to',99,'','{}')"
                )
        finally:
            connection.close()


def test_parallel_initialization_serializes(tmp_path):
    config = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(config) as workspace, SQLiteRuntime(workspace, config) as runtime:

        def open_one():
            connection = runtime.connect()
            try:
                return connection.execute("select workspace_id from settings").get
            finally:
                connection.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            identities = list(pool.map(lambda _: open_one(), range(4)))
        assert len(set(identities)) == 1


def test_failed_initialization_rolls_back_every_table(tmp_path, monkeypatch):
    config = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(config) as workspace, SQLiteRuntime(workspace, config) as runtime:
        original = schema.DDL
        monkeypatch.setattr(schema, "DDL", original + "; invalid sql;")
        with pytest.raises(apsw.SQLError):
            runtime.connect()
        monkeypatch.setattr(schema, "DDL", original)
        connection = runtime.connect()
        try:
            assert connection.execute("select count(*) from settings").get == 1
        finally:
            connection.close()
