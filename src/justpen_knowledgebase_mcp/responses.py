"""Public envelopes and bounded, content-free operational error details."""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

from .errors import VALID_ERROR_TYPES, LimitError, McpError, MissingRecordsError, RecordConflictError, WalBusyError
from .identity import EvidenceID, validate_record_id
from .mutations import canonical_json


class BlockingRecord(BaseModel):
    """One graph or evidence record blocking an operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["nodes", "relations", "evidence"]
    id: UUID | EvidenceID

    @model_validator(mode="after")
    def kind_identity(self) -> Self:
        """Validate blocker ID against its server-selected record kind."""
        validate_record_id(self.kind, str(self.id))
        return self


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
    missing_ids: Annotated[list[UUID | EvidenceID], Field(min_length=1, max_length=100)]


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


def bounded_response(data: dict[str, Any]) -> dict[str, Any]:
    """Enforce the 256 KiB serialized success-envelope budget without changing data."""
    if len(canonical_json(success_response(data)).encode("utf-8")) > 256 * 1024:
        raise LimitError("response exceeds byte budget")
    return data


class TypesResult(BaseModel):
    """Controlled catalog discovery plus optional ready counts."""

    model_config = ConfigDict(extra="forbid")
    types: list[dict[str, Any]] = Field(max_length=100)
    counts_deferred: bool
    common: dict[str, Any]
    formats: dict[str, Any]
    next_cursor: str | None


def tool_output_schema(data_schema: dict[str, Any]) -> dict[str, Any]:
    """Compose the public success/error schema without flattening source DTOs."""
    data = dict(data_schema)
    definitions = dict(data.pop("$defs", {}))
    if "title" in data:
        title = data["title"]
        definitions[title] = data
        data = {"$ref": f"#/$defs/{title}"}
    error = ErrorResult.model_json_schema()
    definitions.update(error.pop("$defs", {}))
    definitions["ErrorResult"] = error
    return {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "properties": {"status": {"const": "ok"}, "data": data},
                "required": ["status", "data"],
                "additionalProperties": False,
            },
            {"$ref": "#/$defs/ErrorResult"},
        ],
        "$defs": definitions,
    }
