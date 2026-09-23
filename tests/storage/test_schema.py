"""Real SQLite schema compatibility and atomic initialization."""

import concurrent.futures
import threading
from contextlib import ExitStack, closing

import apsw
import pytest

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import ConfigurationError, ContractMismatchError, LimitError
from justpen_knowledgebase_mcp.responses import exception_response
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage import schema
from justpen_knowledgebase_mcp.storage.connection import SQLiteRuntime
from justpen_knowledgebase_mcp.workspace import WorkspacePaths

pytestmark = pytest.mark.integration


def test_schema_initialized_and_reopened(tmp_path):
    config = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(config) as workspace, SQLiteRuntime(workspace, config) as runtime:
        identity = None
        with closing(runtime.connect()) as connection:
            assert connection.execute("select schema_version from settings").get == 3
            assert connection.execute("select count(*) from nodes").get == 0
            assert "identity_scope_id" not in {row[1] for row in connection.execute("pragma table_info(nodes)")}
            identity = connection.execute("select workspace_id from settings").get
        with closing(runtime.connect()) as connection:
            assert connection.execute("select workspace_id from settings").get == identity


def test_newer_schema_is_rejected(tmp_path):
    config = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(config) as workspace, SQLiteRuntime(workspace, config) as runtime:
        connection = runtime.connect()
        connection.execute("update settings set schema_version=4")
        connection.close()
        with pytest.raises(ConfigurationError):
            runtime.connect()


def test_v2_workspace_without_the_coverage_column_fails_closed(tmp_path):
    config = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(config) as workspace, SQLiteRuntime(workspace, config) as runtime:
        with closing(runtime.connect()) as connection:
            for suffix in ("insert", "delete", "update"):
                connection.execute(f"drop trigger evidence_coverage_{suffix}")
            connection.execute("alter table settings drop column evidence_coverage")
            connection.execute("update settings set schema_version=2")
        with pytest.raises(ContractMismatchError, match="stored schema version differs"):
            runtime.connect()


def test_v1_catalog_workspace_fails_closed(tmp_path):
    config = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(config) as workspace, SQLiteRuntime(workspace, config) as runtime:
        with closing(runtime.connect()) as connection:
            connection.execute(
                "update settings set schema_version=1,catalog_version=1,catalog_fingerprint=?",
                ("v1-catalog-fingerprint",),
            )
        with pytest.raises(ContractMismatchError, match="stored schema version differs"):
            runtime.connect()


def test_v2_catalog_workspace_fails_closed(tmp_path):
    config = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(config) as workspace, SQLiteRuntime(workspace, config) as runtime:
        with closing(runtime.connect()) as connection:
            connection.execute(
                "update settings set catalog_version=2,catalog_fingerprint=?",
                ("d25e5c37a1e363eccfcadbd7919aac85b017b730380badd765fd3c1a9252c7c9",),
            )
        with pytest.raises(ContractMismatchError, match="stored catalog version differs"):
            runtime.connect()


CONTRACT_BREAKS = [
    ("update settings set schema_version=4", "4", "schema version"),
    ("update settings set catalog_version=0", "0", "catalog version"),
    ("update settings set catalog_fingerprint='wrong-fingerprint'", "wrong-fingerprint", "catalog fingerprint"),
    ("update settings set index_format_version=99", "99", "index format version"),
    ("""update settings set managed_paths='{"data":"elsewhere"}'""", "elsewhere", "managed paths"),
]


@pytest.mark.parametrize(("break_contract", "stored", "dimension"), CONTRACT_BREAKS)
def test_reopen_names_which_contract_dimension_differs(tmp_path, break_contract, stored, dimension):
    config = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(config) as workspace, SQLiteRuntime(workspace, config) as runtime:
        with closing(runtime.connect()) as connection:
            connection.execute(break_contract)
        with pytest.raises(ContractMismatchError) as rejected:
            runtime.connect()
        assert rejected.value.dimension == dimension
        assert stored not in str(rejected.value)


async def test_open_reports_the_differing_dimension_instead_of_maintenance_unavailable(tmp_path):
    # Checkpoint maintenance is the first thing to touch the database at open, so
    # a contract mismatch is classified there before any other reader sees it.
    config = ServerConfig(workspace_dir=tmp_path)
    async with KnowledgeBase.open(config):
        pass
    with (
        WorkspacePaths(config) as workspace,
        SQLiteRuntime(workspace, config) as runtime,
        closing(runtime.connect()) as connection,
    ):
        connection.execute("update settings set catalog_fingerprint='wrong-fingerprint'")
    with pytest.raises(ContractMismatchError) as rejected:
        async with KnowledgeBase.open(config):
            pytest.fail("a broken stored contract must not admit a running service")
    assert rejected.value.dimension == "catalog fingerprint"
    assert exception_response(rejected.value)["error"] == f"CONFIGURATION: {rejected.value}"
    assert "maintenance unavailable" not in str(rejected.value)


@pytest.mark.parametrize("name", ["jobs_active_lane", "nodes_property_fallback", "relations_property_fallback"])
@pytest.mark.parametrize("replace", [False, True])
def test_required_supporting_index_layout_is_checked_on_open(tmp_path, name, replace):
    config = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(config) as workspace, SQLiteRuntime(workspace, config) as runtime:
        with closing(runtime.connect()) as connection:
            connection.execute(f"DROP INDEX {name}")
            if replace:
                table = {
                    "jobs_active_lane": "jobs",
                    "nodes_property_fallback": "nodes",
                    "relations_property_fallback": "relations",
                }[name]
                connection.execute(f"CREATE INDEX {name} ON {table}(id)")
        with pytest.raises(ConfigurationError, match="supporting index"):
            runtime.connect()


def test_unique_identity_and_foreign_keys(tmp_path):
    config = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(config) as workspace, SQLiteRuntime(workspace, config) as runtime:
        connection = runtime.connect()
        try:
            connection.execute("insert into nodes(uuid,type,key,properties) values ('one','ip_address','a','{}')")
            with pytest.raises(apsw.ConstraintError):
                connection.execute("insert into nodes(uuid,type,key,properties) values ('two','ip_address','a','{}')")
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


async def test_owner_intent_constraints_and_sparse_recovery_indexes(tmp_path):

    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:

        def verify(connection, token):
            connection.execute("insert into nodes(uuid,type,key,properties) values ('one','ip_address','a','{}')")
            assert connection.execute("select lifecycle from nodes").get == "ready"
            with pytest.raises(apsw.ConstraintError):
                connection.execute("update nodes set lifecycle='delete_pending'")
            connection.execute(
                "update nodes set lifecycle='delete_pending',delete_job_id='job',delete_cascade=1,delete_requested_at=1"
            )
            with pytest.raises(apsw.ConstraintError):
                connection.execute("update nodes set delete_requested_at=2")
            with pytest.raises(apsw.ConstraintError):
                connection.execute(
                    "update nodes set lifecycle='ready',delete_job_id=null,delete_cascade=null,delete_requested_at=null"
                )
            recovery = list(
                connection.execute(
                    "explain query plan select id from nodes where lifecycle='delete_pending' and id>? order by id limit 100",
                    (0,),
                )
            )
            assert any("nodes_pending_owner" in row[3] for row in recovery)
            lookup = list(connection.execute("explain query plan select lifecycle from nodes where id=?", (1,)))
            assert any("INTEGER PRIMARY KEY" in row[3] for row in lookup)
            connection.execute("delete from nodes")

        await kb.workers.write(verify)


async def test_pending_intent_requires_each_field_and_integer_time(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:

        def verify(connection, token):
            connection.execute("insert into nodes(uuid,type,key,properties) values ('one','ip_address','a','{}')")
            for cascade, timestamp in [(None, 1), (1, None), (1, 1.5)]:
                with pytest.raises(apsw.ConstraintError):
                    connection.execute(
                        "update nodes set lifecycle='delete_pending',delete_job_id='job',delete_cascade=?,delete_requested_at=?",
                        (cascade, timestamp),
                    )

        await kb.workers.write(verify)


def test_bootstrap_serializes_same_workspace_factories_without_blocking_other_workspaces(tmp_path, monkeypatch):

    entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()
    original = schema.SchemaGuard.initialize
    (tmp_path / "shared").mkdir()
    (tmp_path / "other").mkdir()
    with ExitStack() as stack:
        first_config = ServerConfig(workspace_dir=tmp_path / "shared")
        other_config = ServerConfig(workspace_dir=tmp_path / "other")
        first_workspace = stack.enter_context(WorkspacePaths(first_config))
        second_workspace = stack.enter_context(WorkspacePaths(first_config))
        other_workspace = stack.enter_context(WorkspacePaths(other_config))
        first = stack.enter_context(SQLiteRuntime(first_workspace, first_config))
        second = stack.enter_context(SQLiteRuntime(second_workspace, first_config))
        other = stack.enter_context(SQLiteRuntime(other_workspace, other_config))

        def initialize(guard, connection):
            if guard is first.guard:
                entered.set()
                assert release.wait(5)
            if guard is second.guard:
                second_entered.set()
            return original(guard, connection)

        monkeypatch.setattr(schema.SchemaGuard, "initialize", initialize)

        def open_runtime(runtime):
            with closing(runtime.connect()) as connection:
                return connection.execute("select workspace_id from settings").get

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            first_future = pool.submit(open_runtime, first)
            try:
                assert entered.wait(5)
                second_future = pool.submit(open_runtime, second)
                other_future = pool.submit(open_runtime, other)
                assert other_future.result(timeout=5)
                assert not second_entered.wait(0.05)
            finally:
                release.set()
            assert first_future.result(timeout=5) == second_future.result(timeout=5)


def test_bootstrap_wait_expires_before_entering_sql_and_releases_after_failure(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = schema.SchemaGuard.initialize
    config = ServerConfig(workspace_dir=tmp_path)
    short = ServerConfig(workspace_dir=tmp_path, db_busy_timeout_ms=100)
    with (
        WorkspacePaths(config) as workspace,
        SQLiteRuntime(workspace, config) as first,
        SQLiteRuntime(workspace, short) as second,
    ):

        def initialize(guard, connection):
            if guard is first.guard:
                entered.set()
                assert release.wait(5)
            return original(guard, connection)

        monkeypatch.setattr(schema.SchemaGuard, "initialize", initialize)

        def open_first():
            with closing(first.connect()) as connection:
                return connection.execute("select workspace_id from settings").get

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(open_first)
            try:
                assert entered.wait(5)
                with pytest.raises(LimitError, match="startup deadline"):
                    second.connect()
            finally:
                release.set()
            identifier = future.result(timeout=5)
        with closing(second.connect()) as connection:
            assert connection.execute("select workspace_id from settings").get == identifier
