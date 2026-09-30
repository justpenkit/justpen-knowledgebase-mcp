"""Published tool schemas carry the same inventory contract as the request models behind them."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from justpen_knowledgebase_mcp.models import SearchRequest
from justpen_knowledgebase_mcp.tools import graph, search


async def _published(name: str) -> dict[str, Any]:
    mcp = FastMCP("wire-contract")
    graph.register(mcp)
    search.register(mcp)
    tool = await mcp.get_tool(name)
    assert tool is not None
    return tool.parameters


async def test_kb_search_signature_declares_the_search_request_fields():
    """`kb_search` repeats every `SearchRequest` field by hand, so a filter added to one must reach the other."""
    assert set((await _published("kb_search"))["properties"]) == set(SearchRequest.model_fields)


async def test_kb_search_publishes_the_state_vocabularies():
    """A host can refuse an unknown state before sending, as it can an unknown kind."""
    properties = (await _published("kb_search"))["properties"]
    assert {"type": "string", "enum": ["owned", "dependency", "candidate", "rejected"]} in properties["ownership"][
        "anyOf"
    ]
    assert {"type": "string", "enum": ["in_scope", "out_of_scope", "unknown"]} in properties["authorization"]["anyOf"]


async def test_kb_write_publishes_state_on_nodes_only():
    """`kb_write` takes state beside a node's properties; relations stay closed to it."""
    definitions = (await _published("kb_write"))["$defs"]
    assert {"ownership", "authorization", "allowlist_scoped"} <= set(definitions["NodeWrite"]["properties"])
    assert {"ownership", "authorization", "allowlist_scoped"}.isdisjoint(definitions["RelationWrite"]["properties"])
    assert definitions["RelationWrite"]["additionalProperties"] is False
