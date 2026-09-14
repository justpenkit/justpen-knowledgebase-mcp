"""Smoke test for the FastMCP singleton."""

from justpen_knowledgebase_mcp.app import mcp


def test_mcp_singleton_exists():
    assert mcp is not None
    assert mcp.name
