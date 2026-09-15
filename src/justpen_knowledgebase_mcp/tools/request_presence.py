"""Request-scoped explicit argument keys, independent of telemetry."""

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from fastmcp import Context
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from mcp.types import CallToolRequestParams
from pydantic import BaseModel, ValidationError
from typing_extensions import override

from ..errors import InvalidParamsError
from ..responses import bounded_response, exception_response, success_response
from ..service import KnowledgeBase

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_keys: ContextVar[frozenset[str] | None] = ContextVar("kb_argument_keys", default=None)


class RequestPresence(Middleware):
    """Retain only keys while FastMCP validates and injects function defaults."""

    @override
    async def on_call_tool(
        self, context: MiddlewareContext[CallToolRequestParams], call_next: CallNext[CallToolRequestParams, ToolResult]
    ) -> ToolResult:
        """Reset the scope even when a request fails or is cancelled."""
        token = _keys.set(frozenset(context.message.arguments or {}))
        try:
            return await call_next(context)
        finally:
            _keys.reset(token)


def arguments(values: dict[str, Any]) -> dict[str, Any]:
    """Drop injected defaults without altering nested validated model field sets."""
    keys = _keys.get()
    if keys is None:
        raise RuntimeError("request presence is unavailable")
    return {key: value for key, value in values.items() if key in keys}


async def invoke(
    ctx: Context,
    method: str,
    values: dict[str, Any],
    request_model: type[BaseModel] | None = None,
) -> ToolResult:
    """Dispatch a validated request and publish a bounded application envelope."""
    try:
        raw = arguments(values)
        try:
            request = request_model.model_validate(raw) if request_model is not None else raw
        except ValidationError as exc:
            raise InvalidParamsError("invalid tool request") from exc
        operation: Callable[..., Awaitable[dict[str, Any]]] = getattr(get_service(ctx), method)
        data = await operation() if method == "status" else await operation(request)
        envelope = success_response(bounded_response(data))
    # FastMCP exposes unhandled exception messages; hide unexpected content at this boundary.
    except Exception as exc:  # noqa: BLE001
        # Cancellation is a BaseException and must reach the worker/SDK boundary.
        envelope = exception_response(exc)
    return ToolResult(structured_content=envelope, is_error=envelope["status"] == "error")


def get_service(context: Context) -> KnowledgeBase:
    """Resolve the lifespan owner shared by all public tool wrappers."""
    service = context.lifespan_context.get("knowledgebase")
    if not isinstance(service, KnowledgeBase):
        raise TypeError("knowledgebase lifespan is not active")
    return service
