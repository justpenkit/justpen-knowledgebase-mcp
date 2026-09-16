"""Real import/range/link tools preserve bytes and explicit source presence."""

import base64

import pytest
from fastmcp import Client

from justpen_knowledgebase_mcp.app import create_app
from justpen_knowledgebase_mcp.config import ServerConfig

from . import envelope

pytestmark = pytest.mark.integration


async def test_import_exact_bytes_and_paged_sources(tmp_path):
    async with Client(create_app(ServerConfig(workspace_dir=tmp_path))) as client:
        data = b"hello\r\n\xc3\xa4\x00"
        result = await client.call_tool(
            "kb_ingest_evidence", {"base64": base64.b64encode(data).decode(), "source": "first"}
        )
        job = envelope(result)["data"]
        assert job["index_state"] == "not_applicable"
        assert "MEDIA_TYPE_DEFAULTED_TEXT_INDEX_SKIPPED" in job["warnings"]
        identifier = job["evidence_id"]
        read = await client.call_tool("kb_read_evidence", {"evidence_id": identifier, "format": "base64"})
        assert base64.b64decode(envelope(read)["data"]["content"]) == data
        await client.call_tool("kb_ingest_evidence", {"base64": base64.b64encode(data).decode(), "source": "second"})
        first = envelope(
            await client.call_tool("kb_get", {"kind": "evidence", "ids": [identifier], "view": "sources", "limit": 1})
        )["data"]
        assert first["sources"][0]["source"] == "first"
        second = envelope(
            await client.call_tool(
                "kb_get",
                {
                    "kind": "evidence",
                    "ids": [identifier],
                    "view": "sources",
                    "limit": 1,
                    "cursor": first["next_cursor"],
                },
            )
        )["data"]
        assert second["sources"][0]["source"] == "second"
        assert second["next_cursor"] is None
        invalid = await client.call_tool("kb_ingest_evidence", {"text": "body", "path": None}, raise_on_error=False)
        assert invalid.is_error
        assert envelope(invalid)["error"].startswith("INVALID:")
