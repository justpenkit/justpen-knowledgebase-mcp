"""Tool modules. Each exports ``register(mcp)``."""

from fastmcp import FastMCP

from . import echo, fail_demo

__all__ = ["register_all"]


def register_all(mcp: FastMCP) -> None:
    """Register every tool category on the FastMCP instance."""
    echo.register(mcp)
    fail_demo.register(mcp)
