"""Public tool contracts and strict structured-content assertions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp.client.client import CallToolResult


def envelope(result: CallToolResult) -> dict[str, Any]:
    """Require native structured content rather than parsing a text fallback."""
    assert result.structured_content is not None
    assert result.is_error == (result.structured_content["status"] == "error")
    return result.structured_content
