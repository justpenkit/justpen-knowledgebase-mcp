"""Public envelopes and bounded, content-free operational error details."""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_serializer, field_validator

from .errors import VALID_ERROR_TYPES, McpError, MissingRecordsError, RecordConflictError, WalBusyError


class BlockingRecord(BaseModel):
    """One graph or evidence record blocking an operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["nodes", "relations", "evidence"]
    id: UUID


class BlockerDetails(BaseModel):
    """One server-derived dependency/deletion blocker."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    blocking_record: BlockingRecord
    delete_job_id: UUID | None = None
    pending_since: AwareDatetime | None = None
    deletion_owner: BlockingRecord | None = None

    @field_serializer("pending_since")
    def timestamp_output(self, value: datetime | None) -> str | None:
        """Preserve fixed six-digit UTC operational timestamps."""
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z") if value else None


class MissingDetails(BaseModel):
    """Missing IDs from a bounded atomic batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    missing_ids: Annotated[list[UUID], Field(min_length=1, max_length=100)]


class WalRetryDetails(BaseModel):
    """Bounded retry information without SQL, file names or user data."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    reason: Literal["WAL_PRESSURE", "RESET_PENDING", "RESET_IN_PROGRESS", "WAL_RESET_BLOCKED"]
    retry_after_ms: Annotated[int, Field(strict=True, ge=1000, le=30000)]


ErrorDetails = BlockerDetails | MissingDetails | WalRetryDetails


class ErrorResult(BaseModel):
    """Stable error envelope; arbitrary detail objects are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["error"] = "error"
    error: Annotated[str, Field(max_length=1024)]
    details: ErrorDetails | None = None

    @field_validator("error")
    @classmethod
    def valid_code(cls, value: str) -> str:
        """Require a bounded public code followed by a message."""
        if value.split(": ", 1)[0] not in VALID_ERROR_TYPES or ": " not in value:
            raise ValueError("unknown public error code")
        return value


def success_response(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the sibling-MCP success envelope."""
    return {"status": "ok", "data": data if data is not None else {}}


def error_response(error_type: str, message: str, details: ErrorDetails | None = None) -> dict[str, Any]:
    """Build a checked envelope from a server-authored, content-free message."""
    result = ErrorResult(error=f"{error_type}: {message}", details=details).model_dump(mode="json", exclude_none=True)
    if isinstance(details, BlockerDetails):
        result["details"] = details.model_dump(
            mode="json", exclude={"deletion_owner"} if details.deletion_owner is None else set()
        )
    return result


def exception_response(error: BaseException) -> dict[str, Any]:
    """Map expected server-authored errors and hide all unexpected exception data."""
    if isinstance(error, WalBusyError):
        return error_response(
            "BUSY", error.reason, WalRetryDetails(reason=error.reason, retry_after_ms=error.retry_after_ms)
        )
    if isinstance(error, (RecordConflictError, MissingRecordsError)):
        return error_response(error.error_type, str(error), error.details)
    if isinstance(error, McpError):
        message = str(error)
        prefix = error.error_type + ": "
        message = message.removeprefix(prefix)
        return error_response(error.error_type, message)
    return error_response("INTERNAL", "operation failed")
