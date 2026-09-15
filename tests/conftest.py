"""Workspace-local real service fixture, reserved for integration tests."""

import pytest

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.service import KnowledgeBase


@pytest.fixture
async def kb(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as service:
        yield service
