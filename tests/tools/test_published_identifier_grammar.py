"""Every published argument that names a record publishes the spelling the server enforces.

`validate_graph_id` is an `AfterValidator` with no JSON Schema spelling, so `list_tools()` used to
promise a record identifier's 36-byte width and nothing else: a host validating against the
published schema accepted `79693361-7CAE-4DD2-B8E2-101320F37A0E` and the server refused it, one
round trip later. `kb_get.ids`, `kb_delete.ids` and `kb_reindex.ids` were worse than silent. They
publish `RecordID | EvidenceID`, and the evidence branch was an unconstrained string, so the
`anyOf` accepted anything at all.

These tests hold the published constraint to the runtime one at every argument, by walking the real
`list_tools()` output rather than a `TypeAdapter` in isolation: the aliases reach clients through
`anyOf` branches and `list[...]` items, and nothing guarantees a constraint survives that nesting
except looking.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastmcp import Client

from justpen_knowledgebase_mcp.app import create_app
from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.identity import EVIDENCE_ID_PATTERN, GRAPH_ID_PATTERN

from . import envelope

pytestmark = pytest.mark.integration

CANONICAL = "79693361-7cae-4dd2-b8e2-101320f37a0e"
NON_CANONICAL = CANONICAL.upper()
CANONICAL_EVIDENCE = "e_" + "0" * 64
NON_CANONICAL_EVIDENCE = "e_" + "0" * 63 + "F"
# Every published path that carries a client-supplied graph identifier. `kb_write` reaches four of
# them through three nested models, and the three `ids` arguments reach one through an `anyOf`
# branch inside a list item; a constraint that stopped propagating would drop a line from here.
GRAPH_PATHS = [
    "kb_delete.properties.ids.items.anyOf[0]",
    "kb_get.properties.ids.items.anyOf[0]",
    "kb_ingest_evidence.properties.targets.items.properties.id",
    "kb_jobs.properties.job_id.anyOf[0]",
    "kb_neighbors.properties.seed_ids.items",
    "kb_reindex.properties.ids.anyOf[0].items.anyOf[0]",
    "kb_search.properties.source_id.anyOf[0]",
    "kb_search.properties.target_id.anyOf[0]",
    "kb_write.properties.nodes.items.properties.id.anyOf[0]",
    "kb_write.properties.relations.items.properties.id.anyOf[0]",
    "kb_write.properties.relations.items.properties.source_ref.anyOf[0].properties.id.anyOf[0]",
    "kb_write.properties.relations.items.properties.target_ref.anyOf[0].properties.id.anyOf[0]",
]
EVIDENCE_PATHS = [
    "kb_delete.properties.ids.items.anyOf[1]",
    "kb_get.properties.ids.items.anyOf[1]",
    "kb_read_evidence.properties.evidence_id",
    "kb_reindex.properties.ids.anyOf[0].items.anyOf[1]",
    "kb_write.properties.nodes.items.properties.evidence_add.items",
    "kb_write.properties.nodes.items.properties.evidence_remove.items",
    "kb_write.properties.relations.items.properties.evidence_add.items",
    "kb_write.properties.relations.items.properties.evidence_remove.items",
]
# The three arguments whose `anyOf` used to hold an open branch, so the union constrained nothing.
UNION_PATHS = [
    "kb_delete.properties.ids.items",
    "kb_get.properties.ids.items",
    "kb_reindex.properties.ids.anyOf[0].items",
]


@pytest.fixture
async def published(tmp_path) -> dict[str, Any]:
    """Return the real published input schemas, keyed by tool name."""
    async with Client(create_app(ServerConfig(workspace_dir=tmp_path))) as client:
        return {tool.name: tool.input_schema for tool in await client.list_tools()}


@pytest.fixture
async def client(tmp_path):
    async with Client(create_app(ServerConfig(workspace_dir=tmp_path))) as connected:
        yield connected


def _at(published: dict[str, Any], path: str) -> dict[str, Any]:
    """Resolve one `tool.properties.x.items.anyOf[0]` path against the published schemas."""
    tool, _, rest = path.partition(".")
    node: Any = published[tool]
    for step in rest.split("."):
        name, _, index = step.partition("[")
        node = node[name]
        if index:
            node = node[int(index.rstrip("]"))]
    assert isinstance(node, dict)
    return node


def _string_subschemas(node: Any, path: str) -> list[tuple[str, dict[str, Any]]]:
    """Collect every string subschema in a published document, with the path that reaches it."""
    found: list[tuple[str, dict[str, Any]]] = []
    if isinstance(node, dict):
        if node.get("type") == "string":
            found.append((path, node))
        for key, value in node.items():
            found.extend(_string_subschemas(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_string_subschemas(value, f"{path}[{index}]"))
    return found


@pytest.mark.parametrize("path", GRAPH_PATHS)
def test_every_published_graph_identifier_carries_the_canonical_pattern(published, path):
    """The width promise stays, and the spelling the server enforces is published beside it."""
    assert _at(published, path) == {
        "type": "string",
        "minLength": 36,
        "maxLength": 36,
        "pattern": GRAPH_ID_PATTERN,
    }


@pytest.mark.parametrize("path", EVIDENCE_PATHS)
def test_every_published_evidence_identifier_carries_the_canonical_pattern(published, path):
    """An evidence id has no width bound, so the pattern is the only constraint it can publish."""
    assert _at(published, path) == {"type": "string", "pattern": EVIDENCE_ID_PATTERN}


@pytest.mark.parametrize("path", UNION_PATHS)
def test_a_record_or_evidence_union_has_no_unconstrained_branch(published, path):
    """An `anyOf` is only as strict as its loosest branch, and one branch used to accept anything."""
    branches = _at(published, path)["anyOf"]

    assert [branch["pattern"] for branch in branches] == [GRAPH_ID_PATTERN, EVIDENCE_ID_PATTERN]


def test_the_enumerated_paths_are_every_constrained_identifier_the_schemas_publish(published):
    """An argument added without a case above fails here rather than shipping unconstrained."""
    patterned = [
        (path, node["pattern"])
        for tool, schema in sorted(published.items())
        for path, node in _string_subschemas(schema, tool)
        if node.get("pattern") in (GRAPH_ID_PATTERN, EVIDENCE_ID_PATTERN)
    ]

    assert sorted(path for path, pattern in patterned if pattern == GRAPH_ID_PATTERN) == sorted(GRAPH_PATHS)
    assert sorted(path for path, pattern in patterned if pattern == EVIDENCE_ID_PATTERN) == sorted(EVIDENCE_PATHS)


def test_no_published_argument_still_constrains_a_record_identifier_by_width_alone(published):
    """The 36-byte width without the spelling is exactly what let a bad request be worth sending."""
    width_only = [
        path
        for tool, schema in sorted(published.items())
        for path, node in _string_subschemas(schema, tool)
        if node.get("minLength") == 36 and node.get("maxLength") == 36 and "pattern" not in node
    ]

    assert width_only == []


@pytest.mark.parametrize(
    ("path", "accepted", "refused"),
    [(path, CANONICAL, NON_CANONICAL) for path in GRAPH_PATHS]
    + [(path, CANONICAL_EVIDENCE, NON_CANONICAL_EVIDENCE) for path in EVIDENCE_PATHS],
)
def test_a_host_reading_the_published_pattern_reaches_the_server_s_verdict(published, path, accepted, refused):
    """What a client can now decide before sending is the same verdict the server would return.

    `re.fullmatch` is the Python spelling of the anchored ECMA-262 match a host performs;
    `tests/test_identity.py` holds that equivalence over a generated corpus.
    """
    pattern = _at(published, path)["pattern"]

    assert re.fullmatch(pattern, accepted) is not None
    assert re.fullmatch(pattern, refused) is None


async def test_the_refusal_below_the_schema_is_unchanged_by_publishing_it(client):
    """`json_schema_extra` publishes metadata; it must not become a second check with its own words.

    The refusal keeps the layer and the wording `tests/tools/test_signature_envelope.py` pinned: the
    `AfterValidator` names the defect, not a Pydantic `string_should_match_pattern` report.
    """
    refused = await client.call_tool("kb_neighbors", {"seed_ids": [NON_CANONICAL]}, raise_on_error=False)
    evidence = await client.call_tool("kb_read_evidence", {"evidence_id": "e_bad"}, raise_on_error=False)

    assert envelope(refused)["error"] == (
        "INVALID: invalid tool request; seed_ids.0: non-canonical graph id: use the lowercase 8-4-4-4-12 spelling"
    )
    assert envelope(evidence)["error"] == "INVALID: invalid tool request; evidence_id: invalid evidence id"
    assert "should match pattern" not in envelope(refused)["error"]
