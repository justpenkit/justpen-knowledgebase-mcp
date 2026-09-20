"""Factory and actual FastMCP service lifespan."""

from importlib.metadata import version

import pytest
from fastmcp import Client, Context

from justpen_knowledgebase_mcp import app
from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.storage.schema import SCHEMA_VERSION

from .tools import envelope


def test_factory_is_available_without_singleton():
    assert callable(getattr(app, "create_app", None))
    assert not hasattr(app, "mcp")


@pytest.mark.integration
async def test_lifespan_opens_workspace(tmp_path):
    server = app.create_app(ServerConfig(workspace_dir=tmp_path))
    assert not (tmp_path / ".justpen").exists()
    async with Client(server) as client:
        assert len(await client.list_tools()) == 11
        assert (tmp_path / ".justpen/knowledgebase/graph.sqlite3").exists()


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_initialize_reports_application_distribution_version(tmp_path, mode):
    async with Client(app.create_app(ServerConfig(workspace_dir=tmp_path)), mode=mode) as client:
        assert client.server_info is not None
        assert client.server_info.name == "justpen-knowledgebase-mcp"
        assert client.server_info.version == version("justpen-knowledgebase-mcp")


@pytest.mark.integration
async def test_tool_wrapper_resolves_own_lifespan_service(tmp_path):
    server = app.create_app(ServerConfig(workspace_dir=tmp_path))

    @server.tool
    async def probe(ctx: Context) -> int:
        service = app.get_service(ctx)
        return await service.workers.read(lambda c, t: c.execute("select schema_version from settings").get)

    async with Client(server) as client:
        result = await client.call_tool("probe", {})
        assert result.data == SCHEMA_VERSION


@pytest.mark.integration
async def test_tool_contract_and_presence(tmp_path):
    server = app.create_app(ServerConfig(workspace_dir=tmp_path))
    async with Client(server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        assert set(tools) == {
            "kb_status",
            "kb_types",
            "kb_write",
            "kb_get",
            "kb_search",
            "kb_neighbors",
            "kb_delete",
            "kb_ingest_evidence",
            "kb_read_evidence",
            "kb_jobs",
            "kb_reindex",
        }
        assert tools["kb_get"].input_schema["required"] == ["kind", "ids"]
        assert tools["kb_search"].input_schema["properties"]["include_evidence"]["default"] is True
        assert tools["kb_write"].annotations is not None
        assert tools["kb_write"].annotations.idempotent_hint is False
        assert tools["kb_delete"].annotations is not None
        assert tools["kb_delete"].annotations.destructive_hint is True
        for name in ("kb_get", "kb_search", "kb_neighbors", "kb_status", "kb_types", "kb_read_evidence"):
            annotations = tools[name].annotations
            assert annotations is not None
            assert annotations.read_only_hint is True
        result = await client.call_tool("kb_search", {"kind": "evidence"})
        assert envelope(result)["status"] == "ok"
        for value in (False, True):
            result = await client.call_tool(
                "kb_search", {"kind": "evidence", "include_evidence": value}, raise_on_error=False
            )
            assert result.is_error
            assert envelope(result)["error"].startswith("INVALID:")
        result = await client.call_tool("kb_status", {})
        assert envelope(result)["data"]["deployment_scope"] == "single_workspace"


@pytest.mark.integration
async def test_response_schema_keeps_source_ranges_and_error_details(tmp_path):
    async with Client(app.create_app(ServerConfig(workspace_dir=tmp_path))) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        for tool in tools.values():
            assert tool.output_schema is not None
            assert tool.output_schema["type"] == "object"
            error = tool.output_schema["oneOf"][1]
            assert error["properties"]["status"]["const"] == "error"
            assert "details" in error["properties"]
        assert tools["kb_search"].output_schema is not None
        data = tools["kb_search"].output_schema["oneOf"][0]["properties"]["data"]
        assert data["required"][:2] == ["items", "cursor"]
        summary = data["properties"]["items"]["items"]["properties"]
        assert "snippet_range" in summary
        assert "maxLength" not in str(summary["matches"]["items"]["properties"]["pointer"])
