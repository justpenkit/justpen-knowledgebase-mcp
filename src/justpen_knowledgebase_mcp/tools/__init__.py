"""Public tool registration; populated by the graph API implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_all(_mcp: FastMCP) -> None:
    """Keep the registry empty until real knowledgebase tools are implemented."""
