"""Presence is request-local, including nested explicit null fields."""

import asyncio
from uuid import uuid4

import pytest
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools import ToolResult
from mcp.types import CallToolRequestParams
from pydantic import ValidationError

from justpen_knowledgebase_mcp.models import SearchRequest, WriteRequest
from justpen_knowledgebase_mcp.tools import request_presence


def test_nested_patch_presence():
    identifier = str(uuid4())
    request = WriteRequest.model_validate(
        {"nodes": [{"id": identifier}, {"id": identifier, "label": None, "properties": {"literal": None}}]}
    )
    assert request.nodes[0].model_fields_set == {"id"}
    assert request.nodes[1].model_fields_set == {"id", "label", "properties"}
    restored = WriteRequest.model_validate(request.model_dump(exclude_unset=True))
    assert restored.nodes[0].model_fields_set == {"id"}
    assert restored.nodes[1].properties == {"literal": None}


def test_evidence_search_presence():
    assert SearchRequest.model_validate({"kind": "evidence"}).model_fields_set == {"kind"}
    for value in (True, False):
        with pytest.raises(ValidationError):
            SearchRequest.model_validate({"kind": "evidence", "include_evidence": value})


async def test_middleware_concurrency_and_cleanup():

    middleware = request_presence.RequestPresence()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def first(context):
        assert context.message.name == "kb_search"
        entered.set()
        await release.wait()
        assert request_presence.arguments({"kind": "evidence", "include_evidence": True}) == {"kind": "evidence"}
        raise asyncio.CancelledError

    async def second(context):
        assert context.message.name == "kb_search"
        assert request_presence.arguments({"kind": "evidence", "include_evidence": False}) == {
            "kind": "evidence",
            "include_evidence": False,
        }

        return ToolResult(structured_content={})

    one = MiddlewareContext(message=CallToolRequestParams(name="kb_search", arguments={"kind": "evidence"}))
    two = MiddlewareContext(
        message=CallToolRequestParams(name="kb_search", arguments={"kind": "evidence", "include_evidence": False})
    )
    task = asyncio.create_task(middleware.on_call_tool(one, first))
    await entered.wait()
    await middleware.on_call_tool(two, second)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(RuntimeError, match="presence"):
        request_presence.arguments({"kind": "evidence"})
