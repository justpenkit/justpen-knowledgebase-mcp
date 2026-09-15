"""Pure ASGI context propagation before FastMCP creates its native server span."""

from __future__ import annotations

from typing import TYPE_CHECKING

from opentelemetry.context import attach, detach

from .context import extract_carrier, incoming_http

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


class HttpTraceContextMiddleware:
    """Scope context to each HTTP request without reading its body or opening spans."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the public ASGI application."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Restore both context tokens even when the request fails or is cancelled."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        carrier: dict[str, object] = {}
        for name, value in scope.get("headers", []):
            key = name.decode("latin-1").lower()
            if key not in {"traceparent", "tracestate"}:
                continue
            decoded = value.decode("latin-1")
            if key == "traceparent" and key in carrier:
                carrier[key] = None
            elif key == "tracestate" and key in carrier:
                carrier[key] = f"{carrier[key]},{decoded}"
            else:
                carrier[key] = decoded
        incoming = extract_carrier(carrier)
        source_token = incoming_http.set(incoming)
        trace_token = attach(incoming.context)
        try:
            await self.app(scope, receive, send)
        finally:
            detach(trace_token)
            incoming_http.reset(source_token)
