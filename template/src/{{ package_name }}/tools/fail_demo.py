"""Fail-demo tool — demonstrates the error envelope via an McpError raise."""

from fastmcp import FastMCP

from ..errors import DemoFailureError
from ..responses import error_response


def register(mcp: FastMCP) -> None:
    """Register the fail_demo tool on the given FastMCP instance."""

    @mcp.tool
    def fail_demo(should_fail: bool) -> dict[str, object]:  # noqa: FBT001 — MCP tools receive JSON args by name; the bool is not a call-site flag.
        """If should_fail=True, raise DemoFailureError and return the error envelope."""
        if should_fail:
            try:
                raise DemoFailureError("Demo failure triggered")  # noqa: TRY301 — demo intentionally raises-then-catches to show the error-envelope pattern.
            except DemoFailureError as e:
                return error_response(e.error_type, str(e))
        return {"status": "success", "data": {"demo": "ok"}}
