"""Workspace-local real service fixture, reserved for integration tests."""

import json

import pytest

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.connection import SQLiteRuntime
from justpen_knowledgebase_mcp.storage.maintenance import CheckpointMaintenance
from justpen_knowledgebase_mcp.workspace import WorkspacePaths


@pytest.fixture
async def kb(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as service:
        yield service


@pytest.fixture
def small_wal(tmp_path):
    """Bootstrap reduced persisted policy only in this isolated test database."""

    config = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(config) as workspace, SQLiteRuntime(workspace, config) as factory:
        bootstrap = factory.connect()
        policy = json.loads(bootstrap.execute("select policy from settings").get)
        policy.update(
            wal_low_bytes=16384,
            wal_high_bytes=65536,
            journal_size_limit=16384,
            wal_autocheckpoint=0,
            disk_reserve_bytes=1073872896,
        )
        bootstrap.execute("update settings set policy=?", (json.dumps(policy),))
        bootstrap.close()
        connection = factory.open_writer()
        maintenance = CheckpointMaintenance(factory)
        try:
            yield factory, maintenance, connection
        finally:
            maintenance.close_owner()
            connection.close()
