"""Factory and actual FastMCP service lifespan."""

import pytest
from fastmcp import Client, Context

from justpen_knowledgebase_mcp import app
from justpen_knowledgebase_mcp.config import ServerConfig


def test_factory_is_available_without_singleton():
    assert callable(getattr(app, "create_app", None))
    assert not hasattr(app, "mcp")


@pytest.mark.integration
async def test_lifespan_opens_workspace(tmp_path):
    server = app.create_app(ServerConfig(workspace_dir=tmp_path))
    assert not (tmp_path / ".justpen").exists()
    async with Client(server) as client:
        assert await client.list_tools() == []
        assert (tmp_path / ".justpen/knowledgebase/graph.sqlite3").exists()


@pytest.mark.integration
async def test_tool_wrapper_resolves_own_lifespan_service(tmp_path):
    server = app.create_app(ServerConfig(workspace_dir=tmp_path))

    @server.tool
    async def probe(ctx: Context) -> int:
        service = app.get_service(ctx)
        return await service.workers.read(lambda c, t: c.execute("select schema_version from settings").get)

    async with Client(server) as client:
        result = await client.call_tool("probe", {})
        assert result.data == 1
