"""Public maintenance tools; storage operations stay behind the service facade."""

from typing import Annotated, Literal

from fastmcp import Context, FastMCP
from fastmcp.tools import ToolResult
from pydantic import Field, TypeAdapter

from ..evidence import Encoding
from ..models import ClosedModel, EvidenceID, JobResult, Kind, MediaType, RecordID
from ..responses import tool_output_schema
from ..status import StatusResult
from .request_presence import invoke


class JobPage(ClosedModel):
    """Bounded job-list output; each entry retains its reviewed result schema."""

    jobs: list[JobResult] = Field(max_length=100)
    next_cursor: str | None


def register(mcp: FastMCP) -> None:
    """Register this tool family."""

    @mcp.tool(
        output_schema=tool_output_schema(StatusResult.model_json_schema()),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    async def kb_status(ctx: Context) -> ToolResult:
        """Read bounded cached database, WAL, queue, retention and index status. Tool listing is discovery, not a database health check. One configured workspace/engagement is shared by this deployment; requests cannot select a workspace. Samples explicitly report unavailability and staleness."""
        return await invoke(ctx, "status", locals())

    @mcp.tool(
        output_schema=tool_output_schema(TypeAdapter(JobResult | JobPage).json_schema()),
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    )
    async def kb_jobs(
        ctx: Context,
        *,
        action: Literal["list", "get", "cancel", "retry"] = "list",
        job_id: RecordID | None = None,
        state: Literal["queued", "running", "completed", "failed", "cancelled"] | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        cursor: str | None = None,
    ) -> ToolResult:
        """List/get durable jobs or cancel/retry eligible work. Pending delete intent is irreversible and cannot be cancelled. Retry resumes eligible failures; retained terminal jobs may expire, protected deletion jobs do not."""
        return await invoke(ctx, "jobs", locals())

    @mcp.tool(
        output_schema=tool_output_schema(JobResult.model_json_schema()),
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    )
    async def kb_reindex(
        ctx: Context,
        *,
        kind: Kind,
        ids: Annotated[list[RecordID | EvidenceID], Field(min_length=1, max_length=100)] | None = None,
        # FastMCP uses signature names as the fixed public MCP argument names.
        all: bool = False,  # noqa: A002
        media_type: MediaType | None = None,
        encoding: Encoding | None = None,
    ) -> ToolResult:
        """Rebuild derived indexes from current canonical records/evidence without resetting data. Select ids or all=true. Media/encoding override is allowed only for one evidence ID; concurrent generations are fenced and full passes report skipped/busy coverage explicitly."""
        return await invoke(ctx, "reindex", locals())
