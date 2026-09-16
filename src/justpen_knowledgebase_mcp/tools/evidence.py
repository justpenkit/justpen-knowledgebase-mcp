"""Public evidence tools; storage operations stay behind the service facade."""

from typing import Annotated, Literal

from fastmcp import Context, FastMCP
from fastmcp.tools import ToolResult
from pydantic import Field

from ..evidence import Encoding, EvidenceReadResult
from ..models import EvidenceID, JobResult, MediaType, TargetRef
from ..responses import tool_output_schema
from .request_presence import invoke


def register(mcp: FastMCP) -> None:
    """Register this tool family."""

    @mcp.tool(
        output_schema=tool_output_schema(JobResult.model_json_schema()),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    )
    async def kb_ingest_evidence(
        ctx: Context,
        *,
        path: str | None = None,
        text: str | None = None,
        base64: str | None = None,
        media_type: MediaType | None = None,
        encoding: Encoding = "auto",
        source: str | None = None,
        targets: Annotated[list[TargetRef], Field(default_factory=list[TargetRef], max_length=100)],
    ) -> ToolResult:
        """Import exactly one workspace path, inline text or base64 source, optionally linking targets. Content-addressed evidence is preserved byte-for-byte. Declared media_type/encoding controls text indexing; binary defaults may skip indexing. Returns completed or a durable accepted job; inspect warnings and index coverage."""
        return await invoke(ctx, "ingest_evidence", locals())

    @mcp.tool(
        output_schema=tool_output_schema(EvidenceReadResult.model_json_schema()),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    async def kb_read_evidence(
        ctx: Context,
        *,
        evidence_id: EvidenceID,
        offset: Annotated[int, Field(ge=0)] = 0,
        length: Annotated[int, Field(ge=0, le=65536)] = 16384,
        # FastMCP uses signature names as the fixed public MCP argument names.
        format: Literal["text", "base64"] = "text",  # noqa: A002
    ) -> ToolResult:
        """Read an exact bounded raw byte range from ready evidence as text or base64. Offsets and length are byte positions; base64 preserves arbitrary bytes. No managed filesystem path is exposed."""
        return await invoke(ctx, "read_evidence", locals())
