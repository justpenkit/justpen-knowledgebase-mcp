"""Graph wrappers preserve exact canonical updates and bounded errors."""

from uuid import uuid4

import pytest
from fastmcp import Client

from justpen_knowledgebase_mcp.app import create_app
from justpen_knowledgebase_mcp.config import ServerConfig

from . import envelope

pytestmark = pytest.mark.integration


async def test_nested_omitted_null_and_property_values(tmp_path):
    async with Client(create_app(ServerConfig(workspace_dir=tmp_path))) as client:
        props = {"name": "exact.example", "nested": {"null": None, "float": 1.5, "array": [True, 2, "Ä"]}}
        created = await client.call_tool(
            "kb_write", {"nodes": [{"type": "hostname", "properties": props, "label": "keep", "source": "source"}]}
        )
        identifier = envelope(created)["data"]["nodes"][0]["id"]
        await client.call_tool("kb_write", {"nodes": [{"id": identifier}]})
        result = await client.call_tool("kb_get", {"kind": "nodes", "ids": [identifier]})
        record = envelope(result)["data"]["records"][0]
        assert record["properties"] == props
        assert record["label"] == "keep"
        assert record["source"] == "source"
        await client.call_tool(
            "kb_write", {"nodes": [{"id": identifier, "label": None, "source": None, "properties": {"literal": None}}]}
        )
        record = envelope(await client.call_tool("kb_get", {"kind": "nodes", "ids": [identifier]}))["data"]["records"][
            0
        ]
        assert record["label"] is None
        assert record["source"] is None
        assert record["properties"] == {**props, "literal": None}
        duplicate = await client.call_tool(
            "kb_delete", {"kind": "nodes", "ids": [identifier, identifier]}, raise_on_error=False
        )
        assert duplicate.is_error
        assert envelope(duplicate)["error"].startswith("INVALID:")
        missing = str(uuid4())
        error = await client.call_tool(
            "kb_delete", {"kind": "nodes", "ids": [identifier, missing]}, raise_on_error=False
        )
        assert error.is_error
        assert envelope(error)["details"] == {"missing_ids": [missing]}
        assert envelope(error)["error"].startswith("NOT_FOUND:")
        record = envelope(await client.call_tool("kb_get", {"kind": "nodes", "ids": [identifier]}))["data"]["records"][
            0
        ]
        assert record["lifecycle"] == "ready"
        deleted = await client.call_tool("kb_delete", {"kind": "nodes", "ids": [identifier]})
        assert envelope(deleted)["data"]["status"] in {"accepted", "completed"}
        types = await client.call_tool("kb_types", {"kind": "nodes", "type": "hostname"})
        assert envelope(types)["data"]["types"][0]["count"] == 0


async def test_direct_facade_preserves_raw_mapping_fields(kb):
    result = await kb.write(
        {"nodes": [{"type": "hostname", "properties": {"name": "direct.example"}, "label": "keep"}]}
    )
    identifier = result["nodes"][0]["id"]
    await kb.write({"nodes": [{"id": identifier}]})
    record = (await kb.get({"kind": "nodes", "ids": [identifier]}))["records"][0]
    assert record["label"] == "keep"
    await kb.write({"nodes": [{"id": identifier, "label": None}]})
    assert (await kb.get({"kind": "nodes", "ids": [identifier]}))["records"][0]["label"] is None
