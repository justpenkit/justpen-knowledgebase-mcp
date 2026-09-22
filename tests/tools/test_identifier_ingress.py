"""Every public argument that names a graph record refuses a spelling storage cannot match.

SQLite compares TEXT with BINARY collation, so an upper-case spelling of a stored identifier never
matches its row. Before this was refused, `kb_get` answered such a request with `status: ok` and the
identifier listed under `missing_ids`, which reads as "the record does not exist" rather than "you
spelled it wrong". These tests hold every tool argument to the refusal, not only the three that
reached `validate_record_id`.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from justpen_knowledgebase_mcp.app import create_app
from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import InvalidParamsError

from . import envelope

pytestmark = pytest.mark.integration

OTHER = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
# Every tool argument that carries a client-supplied graph identifier, and the spelling to corrupt.
# `NO_RECORD_ARGUMENT` names the rest, so a tool added without a case here fails the guard below.
ARGUMENTS: list[tuple[str, dict[str, object], str]] = [
    ("kb_get", {"kind": "nodes", "ids": ["{identifier}"]}, "ids"),
    ("kb_delete", {"kind": "nodes", "ids": ["{identifier}"]}, "ids"),
    ("kb_reindex", {"kind": "nodes", "ids": ["{identifier}"]}, "ids"),
    ("kb_neighbors", {"seed_ids": ["{identifier}"]}, "seed_ids"),
    ("kb_search", {"kind": "relations", "source_id": "{identifier}"}, "source_id"),
    ("kb_search", {"kind": "relations", "target_id": "{identifier}"}, "target_id"),
    ("kb_jobs", {"action": "get", "job_id": "{identifier}"}, "job_id"),
    ("kb_write", {"nodes": [{"id": "{identifier}", "properties": {"value": "one.example"}}]}, "nodes[].id"),
    (
        "kb_write",
        {
            "relations": [
                {
                    "type": "resolves_to",
                    "properties": {},
                    "source_ref": {"id": "{identifier}"},
                    "target_ref": {"id": OTHER},
                }
            ]
        },
        "relations[].source_ref.id",
    ),
    (
        "kb_ingest_evidence",
        {"text": "body", "media_type": "text/plain", "targets": [{"kind": "nodes", "id": "{identifier}"}]},
        "targets[].id",
    ),
]
NO_RECORD_ARGUMENT = {"kb_types", "kb_status", "kb_read_evidence"}


def _filled(payload: object, identifier: str) -> object:
    if isinstance(payload, dict):
        return {key: _filled(value, identifier) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_filled(item, identifier) for item in payload]
    return identifier if payload == "{identifier}" else payload


@pytest.fixture
async def client(tmp_path):
    async with Client(create_app(ServerConfig(workspace_dir=tmp_path))) as connected:
        yield connected


async def test_the_covered_arguments_are_every_tool_that_takes_a_record_id(client):
    """A new tool with an identifier argument has to be added to the table rather than skipped."""
    registered = {tool.name for tool in await client.list_tools()}

    assert registered == {name for name, _payload, _argument in ARGUMENTS} | NO_RECORD_ARGUMENT


@pytest.mark.parametrize(
    ("tool", "payload", "argument"), ARGUMENTS, ids=[f"{name}.{field}" for name, _p, field in ARGUMENTS]
)
async def test_a_non_canonical_identifier_is_refused_at_every_argument(client, tool, payload, argument):
    del argument
    canonical = await _one_stored_node(client)

    accepted = await client.call_tool(tool, _filled(payload, canonical), raise_on_error=False)
    refused = await client.call_tool(tool, _filled(payload, canonical.upper()), raise_on_error=False)

    assert [block.text for block in refused.content if "non-canonical graph id" in block.text]
    assert refused.is_error
    assert not (accepted.is_error and "non-canonical" in "".join(block.text for block in accepted.content))


async def test_an_existing_record_is_refused_rather_than_reported_missing(client):
    """The defect this closes: the record exists, and the server used to answer that it does not."""
    canonical = await _one_stored_node(client)

    found = envelope(await client.call_tool("kb_get", {"kind": "nodes", "ids": [canonical]}))
    refused = await client.call_tool("kb_get", {"kind": "nodes", "ids": [canonical.upper()]}, raise_on_error=False)

    assert [record["id"] for record in found["data"]["records"]] == [canonical]
    assert refused.is_error
    assert refused.structured_content is None


async def test_the_service_layer_answers_with_the_public_invalid_code(kb):
    """Below the tool signatures the same refusal carries the documented `INVALID` envelope."""
    written = await kb.write({"nodes": [{"type": "domain", "properties": {"value": "service.example"}}]})
    canonical = written["nodes"][0]["id"]

    assert (await kb.get({"kind": "nodes", "ids": [canonical]}))["records"][0]["id"] == canonical
    with pytest.raises(InvalidParamsError, match="invalid get request"):
        await kb.get({"kind": "nodes", "ids": [canonical.upper()]})


async def _one_stored_node(client) -> str:
    result = envelope(
        await client.call_tool("kb_write", {"nodes": [{"type": "domain", "properties": {"value": "one.example"}}]})
    )
    return str(result["data"]["nodes"][0]["id"])
