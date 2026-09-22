"""Request-scoped explicit argument keys, independent of telemetry."""

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from fastmcp import Context
from fastmcp.exceptions import ValidationError as SignatureError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from mcp.types import CallToolRequestParams
from pydantic import BaseModel, ValidationError
from typing_extensions import override

from ..errors import InvalidParamsError
from ..responses import bounded_response, error_response, exception_response, success_response
from ..service import KnowledgeBase

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_keys: ContextVar[frozenset[str] | None] = ContextVar("kb_argument_keys", default=None)
# A rejected argument path is schema-derived except for the one segment that names an argument the
# client invented, which `extra_forbidden` and `unexpected_keyword_argument` report verbatim. Real
# argument names are Python identifiers, so this width keeps every one of them and truncates only a
# key sent to pad the envelope.
_MAX_SEGMENT = 64
_MAX_RULES = 3


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
        # FastMCP answers a signature rejection with a bare `is_error` result carrying a Pydantic
        # report; publish the same envelope the layers below already use, so one refusal has one
        # shape. `kb_get` is where the split showed: an evidence ID under `kind=nodes` was refused
        # by `GetRequest` with an envelope while a non-canonical UUID was refused by the signature
        # without one, and a client could not tell which of the two it would get.
        except SignatureError as exc:
            return ToolResult(structured_content=_signature_response(exc), is_error=True)
        finally:
            _keys.reset(token)


def _signature_response(error: SignatureError) -> dict[str, Any]:
    """Publish the public envelope even when the underlying report cannot be read."""
    try:
        return error_response("INVALID", _signature_rejection(error))
    except Exception:  # noqa: BLE001 — a refusal must still carry the public envelope
        return {"status": "error", "error": "INVALID: invalid tool request"}


def _signature_rejection(error: SignatureError) -> str:
    """Name the rejected arguments and the rules they broke, never the values the client sent.

    Pydantic renders the submitted value into its own report; only `loc` and `msg` are taken here.
    Every validator reachable from a tool signature raises server-authored text, so `msg` carries a
    rule rather than input: the messages that interpolate belong to `catalog.py`, which runs below
    this boundary and already answers with its own envelope.
    """
    cause = error.__cause__
    if not isinstance(cause, ValidationError):
        return "invalid tool request"
    rules: list[str] = []
    for detail in cause.errors()[:_MAX_RULES]:
        # Pydantic names the failing union branch as a path segment; the argument path is the part
        # a client can act on.
        path = ".".join(_segment(item) for item in detail["loc"] if not str(item).startswith("function-"))
        rule = str(detail["msg"]).removeprefix("Value error, ")
        rules.append(f"{path}: {rule}" if path else rule)
    return "; ".join(["invalid tool request", *rules])


def _segment(item: object) -> str:
    """Keep a readable path without echoing an oversized key back to its sender."""
    text = str(item)
    return text if len(text) <= _MAX_SEGMENT else text[:_MAX_SEGMENT] + "..."


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
        try:
            envelope = exception_response(exc)
        except Exception:  # noqa: BLE001 — preserve a safe envelope even if error mapping fails
            envelope = {"status": "error", "error": "INTERNAL: operation failed"}
    return ToolResult(structured_content=envelope, is_error=envelope["status"] == "error")


def get_service(context: Context) -> KnowledgeBase:
    """Resolve the lifespan owner shared by all public tool wrappers."""
    service = context.lifespan_context.get("knowledgebase")
    if not isinstance(service, KnowledgeBase):
        raise TypeError("knowledgebase lifespan is not active")
    return service
