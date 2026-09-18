"""Public graph tools; storage operations stay behind the service facade."""

from typing import Annotated, Literal

from fastmcp import Context, FastMCP
from fastmcp.tools import ToolResult
from pydantic import Field, TypeAdapter

from ..models import (
    DeleteRequest,
    EvidenceID,
    GetRequest,
    GraphKind,
    JobResult,
    Kind,
    LinksViewResult,
    NodeWrite,
    RecordID,
    RecordViewResult,
    RelationWrite,
    SourcesViewResult,
    TypesRequest,
    WriteRequest,
    WriteResult,
)
from ..responses import TypesResult, tool_output_schema
from .request_presence import invoke


def register(mcp: FastMCP) -> None:
    """Register this tool family."""

    @mcp.tool(
        output_schema=tool_output_schema(TypesResult.model_json_schema()),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    async def kb_types(
        ctx: Context,
        *,
        kind: GraphKind,
        # FastMCP uses signature names as the fixed public MCP argument names.
        type: str | None = None,  # noqa: A002
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        cursor: str | None = None,
    ) -> ToolResult:
        """Discover permitted types, required properties, machine-readable identity objects (properties plus optional parent scope), schemas and ready-only counts. Keys are calculated by this MCP; agents cannot define types. Counts may be deferred; list success is not database health."""
        return await invoke(ctx, "types", locals(), TypesRequest)

    @mcp.tool(
        output_schema=tool_output_schema(WriteResult.model_json_schema()),
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    )
    async def kb_write(
        ctx: Context,
        *,
        nodes: Annotated[list[NodeWrite], Field(default_factory=list[NodeWrite], max_length=100)],
        relations: Annotated[list[RelationWrite], Field(default_factory=list[RelationWrite], max_length=100)],
    ) -> ToolResult:
        """Atomically upsert 1-100 nodes/relations using kb_types. New parent-scoped nodes require exactly one declared scope relation in this request; existing scoped IDs do not. The MCP computes keys and forbids re-parenting. ID patches preserve omitted metadata; explicit null clears label/source, while property null is literal data. Endpoints are immutable. Evidence links are explicit."""
        return await invoke(ctx, "write", locals(), WriteRequest)

    @mcp.tool(
        output_schema=tool_output_schema(
            TypeAdapter(RecordViewResult | LinksViewResult | SourcesViewResult).json_schema()
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    async def kb_get(
        ctx: Context,
        *,
        kind: Kind,
        ids: Annotated[list[RecordID | EvidenceID], Field(min_length=1, max_length=100)],
        view: Literal["record", "links", "sources"] = "record",
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        cursor: str | None = None,
    ) -> ToolResult:
        """Read full canonical properties and metadata, with remaining_ids when the response budget is reached. Record view includes counts; links/sources views page one owner with a bound cursor. Sources is evidence-only; pending lifecycle can be inspected."""
        return await invoke(ctx, "get", locals(), GetRequest)

    @mcp.tool(
        output_schema=tool_output_schema(JobResult.model_json_schema()),
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    )
    async def kb_delete(
        ctx: Context,
        *,
        kind: Kind,
        ids: Annotated[list[RecordID | EvidenceID], Field(min_length=1, max_length=100)],
        cascade: bool = False,
    ) -> ToolResult:
        """Atomically admit 1-100 unique IDs for durable deletion. Missing or pending targets reject the whole batch. cascade=false rejects linked records; cascade=true removes incident relations/links, preserving neighboring nodes and evidence blobs unless evidence itself is selected. Accepted jobs remain durable; pending deletion cannot be cancelled."""
        return await invoke(ctx, "delete", locals(), DeleteRequest)
