"""Public search tools; storage operations stay behind the service facade."""

from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from fastmcp.tools import ToolResult
from pydantic import Field

from ..models import Kind, MediaType, NeighborsResult, RecordID, SearchResult
from ..responses import tool_output_schema
from .request_presence import invoke


def register(mcp: FastMCP) -> None:
    """Register this tool family."""

    @mcp.tool(
        output_schema=tool_output_schema(SearchResult.model_json_schema()),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    async def kb_search(
        ctx: Context,
        *,
        kind: Kind,
        query: str | None = None,
        query_mode: Literal["literal", "words"] = "literal",
        include_evidence: bool = True,
        sort: Literal["id", "relevance"] = "id",
        media_type: MediaType | None = None,
        index_state: Literal["pending", "ready", "not_applicable", "index_failed"] | None = None,
        byte_size_min: Annotated[int, Field(ge=0)] | None = None,
        byte_size_max: Annotated[int, Field(ge=0)] | None = None,
        created_at_min: str | None = None,
        created_at_max: str | None = None,
        # FastMCP uses signature names as the fixed public MCP argument names.
        type: str | None = None,  # noqa: A002
        key: str | None = None,
        source: str | None = None,
        source_id: RecordID | None = None,
        target_id: RecordID | None = None,
        observed_at_min: str | None = None,
        observed_at_max: str | None = None,
        properties: dict[str, Any] | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        cursor: str | None = None,
    ) -> ToolResult:
        """Search ready summaries using exact property filters and literal/words text. Include linked evidence only for graph searches; omit include_evidence for kind=evidence. Canonical fallback preserves unindexed property correctness. Coverage reports pending/failed/incomplete text indexes; use kb_get/kb_read_evidence for full content."""
        return await invoke(ctx, "search", locals())

    @mcp.tool(
        output_schema=tool_output_schema(NeighborsResult.model_json_schema()),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    async def kb_neighbors(
        ctx: Context,
        *,
        seed_ids: Annotated[list[RecordID], Field(min_length=1, max_length=1000)],
        direction: Literal["in", "out", "both"] = "both",
        relation_types: Annotated[list[str], Field(max_length=100)] | None = None,
        depth: Annotated[int, Field(ge=0, le=3)] = 1,
        max_nodes: Annotated[int, Field(ge=1, le=1000)] = 100,
        max_edges: Annotated[int, Field(ge=0, le=3000)] = 300,
    ) -> ToolResult:
        """Traverse stored ready relations from seed_ids within depth/node/edge/response/deadline budgets. Returns explicit truncation and frontier; pending deletion may temporarily disconnect the graph. No inferred relations are created."""
        return await invoke(ctx, "neighbors", locals())
