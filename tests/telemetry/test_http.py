import asyncio
from unittest.mock import AsyncMock

import pytest
from opentelemetry import trace
from opentelemetry.context import attach, detach

from justpen_knowledgebase_mcp.telemetry.context import extract_carrier, incoming_http
from justpen_knowledgebase_mcp.telemetry.http import HttpTraceContextMiddleware

PARENT = "00-11111111111111111111111111111111-2222222222222222-01"
OTHER = "00-33333333333333333333333333333333-4444444444444444-01"


@pytest.mark.parametrize("failure", [None, ValueError, asyncio.CancelledError])
async def test_context_is_restored_and_body_is_untouched(failure):
    receive, send = AsyncMock(), AsyncMock()

    async def app(scope, actual_receive, actual_send):
        assert actual_receive is receive
        assert actual_send is send
        assert trace.get_current_span().get_span_context().trace_id == int("11" * 16, 16)
        incoming = incoming_http.get()
        assert incoming is not None
        assert incoming.valid
        if failure:
            raise failure

    token = attach(extract_carrier({"traceparent": OTHER}).context)
    try:
        middleware = HttpTraceContextMiddleware(app)
        scope = {"type": "http", "headers": [(b"traceparent", PARENT.encode())]}
        if failure:
            with pytest.raises(failure):
                await middleware(scope, receive, send)
        else:
            await middleware(scope, receive, send)
        assert trace.get_current_span().get_span_context().trace_id == int("33" * 16, 16)
        assert incoming_http.get() is None
        receive.assert_not_called()
    finally:
        detach(token)


async def test_concurrent_http_contexts_are_isolated():
    arrived = 0
    barrier = asyncio.Event()
    observed = {}

    async def app(scope, receive, send):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            barrier.set()
        await asyncio.wait_for(barrier.wait(), 2)
        observed[scope["path"]] = trace.get_current_span().get_span_context().trace_id

    middleware = HttpTraceContextMiddleware(app)
    await asyncio.gather(
        *(
            middleware(
                {"type": "http", "path": path, "headers": [(b"traceparent", parent.encode())]}, AsyncMock(), AsyncMock()
            )
            for path, parent in [("/a", PARENT), ("/b", OTHER)]
        )
    )
    assert observed == {"/a": int("11" * 16, 16), "/b": int("33" * 16, 16)}


async def test_duplicate_parent_is_invalid_and_clears_ambient_parent():
    async def app(scope, receive, send):
        incoming = incoming_http.get()
        assert incoming is not None
        assert incoming.invalid
        assert not incoming.valid
        assert not trace.get_current_span().get_span_context().is_valid

    token = attach(extract_carrier({"traceparent": OTHER}).context)
    try:
        await HttpTraceContextMiddleware(app)(
            {"type": "http", "headers": [(b"traceparent", PARENT.encode())] * 2}, AsyncMock(), AsyncMock()
        )
    finally:
        detach(token)


@pytest.mark.parametrize("scope_type", ["websocket", "lifespan"])
async def test_non_http_scope_passes_through(scope_type):
    app = AsyncMock()
    scope, receive, send = {"type": scope_type}, AsyncMock(), AsyncMock()
    await HttpTraceContextMiddleware(app)(scope, receive, send)
    app.assert_awaited_once_with(scope, receive, send)


async def test_split_tracestate_headers_preserve_one_bounded_w3c_value():
    observed = []

    async def app(scope, receive, send):
        observed.append(trace.get_current_span().get_span_context().trace_state.to_header())

    await HttpTraceContextMiddleware(app)(
        {
            "type": "http",
            "headers": [(b"traceparent", PARENT.encode()), (b"tracestate", b"one=a"), (b"tracestate", b"two=b")],
        },
        AsyncMock(),
        AsyncMock(),
    )
    assert observed == ["one=a,two=b"]
