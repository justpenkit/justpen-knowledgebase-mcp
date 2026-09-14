"""Standard response envelope builders."""

from typing import Any

from .errors import VALID_ERROR_TYPES


def success_response(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a success envelope.

    Args:
        data: Tool-specific payload. Defaults to an empty dict.

    Returns:
        ``{"status": "success", "data": {...}}``
    """
    return {"status": "success", "data": data if data is not None else {}}


def error_response(error_type: str, message: str) -> dict[str, Any]:
    """Build an error envelope.

    Args:
        error_type: One of the values in ``VALID_ERROR_TYPES``.
        message: Human-readable, action-oriented description.

    Returns:
        ``{"status": "error", "error_type": <type>, "message": <msg>}``

    Raises:
        ValueError: If ``error_type`` is not in ``VALID_ERROR_TYPES``.
    """
    if error_type not in VALID_ERROR_TYPES:
        raise ValueError(f"Unknown error_type '{error_type}'. Valid: {', '.join(sorted(VALID_ERROR_TYPES))}")
    return {"status": "error", "error_type": error_type, "message": message}
