"""Real search/traversal summaries retain exact text references and links."""

import pytest
from fastmcp import Client

from justpen_knowledgebase_mcp.app import create_app
from justpen_knowledgebase_mcp.config import ServerConfig

from . import envelope

pytestmark = pytest.mark.integration


async def test_search_and_neighbors_use_reviewed_wire_shapes(tmp_path):
    async with Client(create_app(ServerConfig(workspace_dir=tmp_path))) as client:
        result = await client.call_tool(
            "kb_write",
            {
                "nodes": [
                    {
                        "type": "application",
                        "properties": {"sha256": "a" * 64, "platform": "linux", "note": "alpha proof"},
                    },
                    {"type": "endpoint", "properties": {"url": "https://example.test/", "method": "GET"}},
                ],
                "relations": [
                    {
                        "type": "contacts",
                        "source_ref": {"node_index": 0},
                        "target_ref": {"node_index": 1},
                        "properties": {"context": "production", "basis": "static"},
                    }
                ],
            },
        )
        identifiers = [entry["id"] for entry in envelope(result)["data"]["nodes"]]
        search = envelope(await client.call_tool("kb_search", {"kind": "nodes", "query": "alpha"}))["data"]
        assert set(search) == {
            "items",
            "cursor",
            "has_more",
            "property_filter_mode",
            "canonical_scan_count",
            "incomplete",
            "coverage",
        }
        assert search["items"][0]["id"] == identifiers[0]
        assert search["items"][0]["matches"][0]["pointer"] == "/properties/note"
        assert search["items"][0]["snippet_range"]["byte_start"] == 0
        assert "properties" not in search["items"][0]
        neighbors = envelope(await client.call_tool("kb_neighbors", {"seed_ids": [identifiers[0]]}))["data"]
        assert {node["id"] for node in neighbors["nodes"]} == set(identifiers)
        assert len(neighbors["edges"]) == 1
        assert neighbors["truncated"] is False
