"""Custom exceptions. Each maps 1:1 to an error_type in responses."""


class McpError(Exception):
    """Base class for all server exceptions."""

    error_type: str = "internal_error"


class InvalidParamsError(McpError):
    """Tool input fails validation."""

    error_type = "invalid_params"


class InternalError(McpError):
    """Unexpected internal failure not covered by a more specific type."""

    error_type = "internal_error"


class DemoFailureError(McpError):
    """Raised by the fail_demo tool. Delete when you remove the demo."""

    error_type = "demo_failure"


VALID_ERROR_TYPES = frozenset({"invalid_params", "internal_error", "demo_failure"})
