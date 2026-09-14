"""Echo tool — demonstrates the success envelope pattern."""

from fastmcp import FastMCP

from ..responses import success_response


def register(mcp: FastMCP) -> None:
    """Register the echo tool on the given FastMCP instance."""

    @mcp.tool
    def echo(message: str) -> dict[str, object]:
        """Return the input message wrapped in the success envelope."""
        return success_response({"echoed": message})
