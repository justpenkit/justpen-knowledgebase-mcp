"""Tool request boundary, sanitizer and sampler with SDK/DB collaborators isolated."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from justpen_knowledgebase_mcp import status, tools
from justpen_knowledgebase_mcp.config import WorkspacePolicy
from justpen_knowledgebase_mcp.models import GetRequest
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage import status as storage_status
from justpen_knowledgebase_mcp.tools import evidence, graph, maintenance, request_presence, search

from .helpers import NODE, cursor, database


async def test_presence_dispatch_validates_then_drops_injected_defaults(monkeypatch):
    operation = AsyncMock(return_value={"records": []})
    monkeypatch.setattr(request_presence, "get_service", Mock(return_value=Mock(get=operation)))
    middleware = request_presence.RequestPresence()
    context = Mock(message=Mock(arguments={"kind": "nodes", "ids": [NODE]}))

    async def call(context):
        del context
        return await request_presence.invoke(
            Mock(), "get", {"kind": "nodes", "ids": [NODE], "view": "record", "limit": 20}, GetRequest
        )

    result = await middleware.on_call_tool(context, call)
    assert result.structured_content is not None
    assert result.structured_content["status"] == "ok"
    validated = operation.call_args.args[0]
    assert validated.model_fields_set == {"kind", "ids"}
    with pytest.raises(RuntimeError, match="unavailable"):
        request_presence.arguments({})


@pytest.mark.parametrize("error", [ValueError("secret value"), OSError("secret path")])
async def test_tool_exception_boundary_does_not_leak_internal_content(monkeypatch, error):
    monkeypatch.setattr(request_presence, "get_service", Mock(return_value=Mock(status=AsyncMock(side_effect=error))))

    async def call(context):
        del context
        return await request_presence.invoke(Mock(), "status", {})

    result = await request_presence.RequestPresence().on_call_tool(Mock(message=Mock(arguments={})), call)
    assert result.is_error
    assert "secret" not in str(result.structured_content)


async def test_mapper_failure_still_returns_private_safe_tool_envelope(monkeypatch):
    monkeypatch.setattr(
        request_presence,
        "get_service",
        Mock(return_value=Mock(status=AsyncMock(side_effect=ValueError("secret path")))),
    )
    monkeypatch.setattr(request_presence, "exception_response", Mock(side_effect=ValueError("mapper secret")))

    async def call(context):
        del context
        return await request_presence.invoke(Mock(), "status", {})

    result = await request_presence.RequestPresence().on_call_tool(Mock(message=Mock(arguments={})), call)
    assert result.is_error
    assert result.structured_content == {"status": "error", "error": "INTERNAL: operation failed"}


async def test_invoke_cancellation_is_not_mapped_to_an_error(monkeypatch):
    monkeypatch.setattr(
        request_presence, "get_service", Mock(return_value=Mock(status=AsyncMock(side_effect=asyncio.CancelledError)))
    )

    async def call(context):
        del context
        return await request_presence.invoke(Mock(), "status", {})

    with pytest.raises(asyncio.CancelledError):
        await request_presence.RequestPresence().on_call_tool(Mock(message=Mock(arguments={})), call)


async def test_invalid_tool_model_never_dispatches(monkeypatch):
    service = Mock()
    monkeypatch.setattr(request_presence, "get_service", service)

    async def call(context):
        del context
        return await request_presence.invoke(Mock(), "get", {"kind": "invalid"}, GetRequest)

    result = await request_presence.RequestPresence().on_call_tool(
        Mock(message=Mock(arguments={"kind": "invalid"})), call
    )
    assert result.is_error
    service.assert_not_called()


async def test_status_sampler_retains_prior_sample_and_sanitizes_failure(monkeypatch):
    coverage = {"ready": 1, "pending": 0, "failed": 0, "incomplete": 0, "not_applicable": 0}
    monkeypatch.setattr(storage_status, "coverage", Mock(return_value=coverage))
    db = database(
        cursor(rows=[(1, 1, 1, WorkspacePolicy().model_dump_json())]),
        cursor(rows=[("running", 2)]),
        cursor(value=3),
        cursor(value=4),
    )
    sample = storage_status.sample_status(db, Mock())
    assert sample["jobs"]["running"] == 2
    assert sample["property_index_fallback"] == {"nodes": 3, "relations": 4}
    reader = AsyncMock(return_value=sample)
    sampler = status.StatusSampler(Mock(read=reader))
    await sampler.start()
    try:
        before = sampler.snapshot()
        assert before["available"]
        reader.side_effect = OSError("private database path")
        await sampler.refresh()
        after = sampler.snapshot()
        assert after["sample"] == before["sample"]
        assert after["stale"]
        assert after["last_error"] == "IO_ERROR"
    finally:
        await sampler.close()


async def test_all_public_wrappers_route_to_facade_with_sdk_registration_isolated(monkeypatch):

    entries = {}

    def tool(**settings):
        def register(function):
            entries[function.__name__] = (function, settings)
            return function

        return register

    mcp = Mock(tool=tool)
    dispatch = AsyncMock(return_value="tool-result")
    for family in (evidence, graph, maintenance, search):
        monkeypatch.setattr(family, "invoke", dispatch)
    tools.register_all(mcp)
    expected = {
        "kb_types": ("types", {"kind": "nodes"}),
        "kb_write": ("write", {"nodes": [], "relations": []}),
        "kb_get": ("get", {"kind": "nodes", "ids": [NODE]}),
        "kb_delete": ("delete", {"kind": "nodes", "ids": [NODE]}),
        "kb_ingest_evidence": ("ingest_evidence", {"text": "abc", "targets": []}),
        "kb_read_evidence": ("read_evidence", {"evidence_id": "e_" + "a" * 64}),
        "kb_search": ("search", {"kind": "nodes"}),
        "kb_neighbors": ("neighbors", {"seed_ids": [NODE]}),
        "kb_status": ("status", {}),
        "kb_jobs": ("jobs", {}),
        "kb_reindex": ("reindex", {"kind": "nodes"}),
    }
    assert entries.keys() == expected.keys()
    context = Mock()
    for name, (method, arguments) in expected.items():
        function, settings = entries[name]
        assert await function(context, **arguments) == "tool-result"
        assert dispatch.call_args.args[:2] == (context, method)
        assert all(dispatch.call_args.args[2][key] == value for key, value in arguments.items())
        assert "output_schema" in settings
    mcp.add_middleware.assert_called_once()


async def test_presence_cancellation_resets_context_and_service_requires_lifespan():

    async def cancelled(context):
        del context
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await request_presence.RequestPresence().on_call_tool(Mock(message=Mock(arguments={})), cancelled)
    with pytest.raises(RuntimeError):
        request_presence.arguments({})
    with pytest.raises(TypeError):
        request_presence.get_service(Mock(lifespan_context={}))
    kb = object.__new__(KnowledgeBase)
    assert request_presence.get_service(Mock(lifespan_context={"knowledgebase": kb})) is kb
