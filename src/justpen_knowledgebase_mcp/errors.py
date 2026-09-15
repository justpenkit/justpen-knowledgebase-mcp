"""Bounded public failure codes; exception messages must be content-free."""

from typing import Literal

WalBusyReason = Literal["WAL_PRESSURE", "RESET_PENDING", "RESET_IN_PROGRESS", "WAL_RESET_BLOCKED"]

VALID_ERROR_TYPES = frozenset(
    {
        "INVALID",
        "NOT_FOUND",
        "CONFLICT",
        "BUSY",
        "LIMIT",
        "PATH_DENIED",
        "IO_ERROR",
        "INDEX_ERROR",
        "CANCELLED",
        "CONFIGURATION",
        "INTERNAL",
    }
)


class McpError(Exception):
    """Base server error with a stable public code."""

    error_type: str = "INTERNAL"


class InvalidParamsError(McpError):
    """Invalid public input."""

    error_type = "INVALID"


class InternalError(McpError):
    """An unexpected failure, without its internal details."""


class ConfigurationError(McpError):
    """Incompatible configuration or stored contract."""

    error_type = "CONFIGURATION"


class PathDeniedError(McpError):
    """Workspace containment rejected the path."""

    error_type = "PATH_DENIED"


class StorageIOError(McpError):
    """Managed storage failed."""

    error_type = "IO_ERROR"


class BusyError(McpError):
    """Bounded capacity or SQLite lock is unavailable."""

    error_type = "BUSY"


class WalBusyError(BusyError):
    """Server-derived WAL rejection metadata captured at admission time."""

    def __init__(self, reason: WalBusyReason, retry_after_ms: int) -> None:
        """Validate bounded content-free fields without depending on responses."""
        if reason not in ("WAL_PRESSURE", "RESET_PENDING", "RESET_IN_PROGRESS", "WAL_RESET_BLOCKED"):
            raise ValueError("invalid WAL reason")
        if type(retry_after_ms) is not int or not 1000 <= retry_after_ms <= 30000:
            raise ValueError("invalid WAL retry delay")
        self._reason: WalBusyReason = reason
        self._retry_after_ms = retry_after_ms
        super().__init__(reason)

    @property
    def reason(self) -> WalBusyReason:
        """Return the immutable server-authored reason."""
        return self._reason

    @property
    def retry_after_ms(self) -> int:
        """Return retry guidance, never a completion estimate."""
        return self._retry_after_ms


class LimitError(McpError):
    """The absolute operation budget expired."""

    error_type = "LIMIT"


class CancelledOperationError(McpError):
    """Cancellation was accepted before the commit boundary."""

    error_type = "CANCELLED"


class NotFoundError(McpError):
    """A requested public record does not exist."""

    error_type = "NOT_FOUND"


class ConflictError(McpError):
    """The operation conflicts with current record state."""

    error_type = "CONFLICT"


class IndexingError(McpError):
    """Derived indexing failed without exposing its input."""

    error_type = "INDEX_ERROR"
