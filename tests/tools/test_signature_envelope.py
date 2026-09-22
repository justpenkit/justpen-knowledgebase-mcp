"""One refusal, one shape: a signature rejection carries the envelope the layers below already use.

FastMCP validates a tool's published signature before the request model is built, and answered a
failure with a bare `is_error` result carrying a Pydantic report instead of an application envelope.
`kb_get` is where that split cost a client something real: an evidence ID under `kind=nodes` is
refused by `GetRequest` with `INVALID: invalid tool request`, while a non-canonical UUID in the same
argument was refused one layer earlier with no `structured_content` at all. Both mean "this argument
cannot name a record", and nothing in the published schema told a client which shape it would get.

The envelope must not cost the diagnosis that the Pydantic report carried, so it names the rejected
argument path and the rule. It must not gain what that report leaked either: the value the client
sent stays out of it.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from justpen_knowledgebase_mcp.app import create_app
from justpen_knowledgebase_mcp.config import ServerConfig

from . import envelope

pytestmark = pytest.mark.integration

EVIDENCE = "e_" + "0" * 64


@pytest.fixture
async def client(tmp_path):
    async with Client(create_app(ServerConfig(workspace_dir=tmp_path))) as connected:
        yield connected


async def _one_stored_node(client) -> str:
    result = envelope(
        await client.call_tool("kb_write", {"nodes": [{"type": "domain", "properties": {"value": "one.example"}}]})
    )
    return str(result["data"]["nodes"][0]["id"])


async def test_both_identifier_rules_of_one_argument_answer_with_one_envelope(client):
    """The split this closes: `kb_get.ids` refused with an envelope, or without one, by luck of rule."""
    canonical = await _one_stored_node(client)

    below = await client.call_tool("kb_get", {"kind": "nodes", "ids": [EVIDENCE]}, raise_on_error=False)
    signature = await client.call_tool("kb_get", {"kind": "nodes", "ids": [canonical.upper()]}, raise_on_error=False)

    assert below.is_error
    assert signature.is_error
    assert envelope(below)["error"].startswith("INVALID: invalid tool request")
    assert envelope(signature)["error"].startswith("INVALID: invalid tool request")


@pytest.mark.parametrize(
    ("payload", "path", "rule"),
    [
        ({"kind": "bogus", "ids": [EVIDENCE]}, "kind", "Input should be 'nodes', 'relations' or 'evidence'"),
        ({"kind": "nodes"}, "ids", "Missing required keyword only argument"),
        ({"kind": "nodes", "ids": [EVIDENCE], "bogus": 1}, "bogus", "Unexpected keyword argument"),
        ({"kind": "nodes", "ids": ["z" * 36]}, "ids.0", "invalid graph id"),
        ({"kind": "nodes", "ids": ["z" * 35]}, "ids.0", "String should have at least 36 characters"),
    ],
    ids=["literal", "missing", "extra", "rule", "width"],
)
async def test_a_signature_refusal_names_the_argument_and_the_rule(client, payload, path, rule):
    """Every signature rejection, not only an identifier, publishes the envelope with its diagnosis."""
    refused = await client.call_tool("kb_get", payload, raise_on_error=False)

    assert refused.is_error
    assert f"{path}: {rule}" in envelope(refused)["error"]


async def test_a_refusal_never_repeats_the_value_the_client_sent(client):
    """Pydantic renders `input_value` into its report; the envelope carries the rule instead."""
    canonical = await _one_stored_node(client)

    refused = await client.call_tool("kb_neighbors", {"seed_ids": [canonical.upper()]}, raise_on_error=False)

    error = envelope(refused)["error"]
    assert "non-canonical graph id: use the lowercase 8-4-4-4-12 spelling" in error
    assert canonical.upper() not in error


async def test_an_invented_argument_name_cannot_pad_the_envelope(client):
    """The one path segment a client authors is bounded rather than echoed at full length."""
    refused = await client.call_tool("kb_get", {"kind": "nodes", "ids": [EVIDENCE], "k" * 200: 1}, raise_on_error=False)

    error = envelope(refused)["error"]
    assert "k" * 64 + "...: Unexpected keyword argument" in error
    assert "k" * 65 not in error


async def test_a_call_that_reaches_no_tool_still_has_no_envelope(client):
    """The residual `docs/tools/index.md` keeps: a refusal above the tool cannot carry one."""
    refused = await client.call_tool("kb_bogus", {}, raise_on_error=False)

    assert refused.is_error
    assert refused.structured_content is None


async def test_the_published_schema_still_constrains_a_record_identifier(client):
    """Guard the route not taken: validating inside the function would drop this from the schema.

    Loosening a signature to plain `str` and checking the spelling in the tool body would produce
    the same envelope while removing what an MCP host can check before sending. These are the
    constraints `list_tools()` published before the envelope existed, and they are unchanged.
    """
    published = {tool.name: tool.input_schema for tool in await client.list_tools()}

    assert published["kb_neighbors"]["properties"]["seed_ids"]["items"] == {
        "type": "string",
        "minLength": 36,
        "maxLength": 36,
    }
    assert {"type": "string", "minLength": 36, "maxLength": 36} in published["kb_search"]["properties"]["source_id"][
        "anyOf"
    ]
    assert {"type": "string", "minLength": 36, "maxLength": 36} in published["kb_get"]["properties"]["ids"]["items"][
        "anyOf"
    ]
