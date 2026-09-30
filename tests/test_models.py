"""Closed strict request models preserve nested raw field presence."""

from typing import Annotated, Any, get_args, get_origin, get_type_hints
from uuid import UUID, uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from justpen_knowledgebase_mcp import models
from justpen_knowledgebase_mcp.catalog import catalog_manifest
from justpen_knowledgebase_mcp.identity import GRAPH_ID_PATTERN
from justpen_knowledgebase_mcp.jobs import JobsRequest
from justpen_knowledgebase_mcp.models import (
    ClosedModel,
    DeleteRequest,
    GetRequest,
    MutationResult,
    NeighborEdge,
    NeighborNode,
    NeighborsRequest,
    NodeRef,
    NodeWrite,
    RelationWrite,
    SearchRequest,
    SearchSummary,
    StoredRecordID,
    TargetRef,
    WriteRequest,
    WriteResult,
)
from justpen_knowledgebase_mcp.reindex import ReindexRequest


@pytest.mark.parametrize("value", [{}, {"id": str(uuid4()), "node_index": 0}, {"node_index": True}, {"node_index": -1}])
def test_node_ref_exactly_one(value):
    with pytest.raises(ValidationError):
        NodeRef.model_validate(value)


@pytest.mark.parametrize("field", ["key", "identity", "lifecycle"])
def test_server_fields_closed(field):
    with pytest.raises(ValidationError):
        NodeWrite.model_validate({"type": "ip_address", "properties": {"value": "192.0.2.1", "version": 4}, field: "x"})


def test_nested_presence_and_null_survive():
    model = WriteRequest.model_validate(
        {
            "nodes": [
                {
                    "type": "ip_address",
                    "properties": {"value": "192.0.2.1", "version": 4, "extra": None},
                    "label": None,
                }
            ]
        }
    )
    assert "label" in model.nodes[0].model_fields_set
    assert "source" not in model.nodes[0].model_fields_set
    assert model.model_dump(exclude_unset=True)["nodes"][0]["properties"]["extra"] is None


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "nodes", "ids": [str(uuid4())], "view": "sources"},
        {"kind": "nodes", "ids": [str(uuid4()), str(uuid4())], "view": "links"},
        {"kind": "nodes", "ids": [], "view": "record"},
    ],
)
def test_get_view_constraints(value):
    with pytest.raises(ValidationError):
        GetRequest.model_validate(value)


def test_write_and_link_request_caps():
    with pytest.raises(ValidationError):
        WriteRequest.model_validate({"nodes": [{"id": str(uuid4())}] * 101})
    with pytest.raises(ValidationError):
        WriteRequest.model_validate(
            {"nodes": [{"id": str(uuid4()), "evidence_add": [str(uuid4()) for _ in range(101)]}]}
        )


def test_delete_duplicate_ids_rejected():
    identifier = str(uuid4())
    with pytest.raises(ValidationError):
        DeleteRequest.model_validate({"kind": "nodes", "ids": [identifier, identifier]})


def test_result_models_are_closed():

    result = WriteResult.model_validate(
        {
            "nodes": [
                {
                    "id": str(uuid4()),
                    "created": True,
                    "updated": False,
                    "links_added": 0,
                    "links_removed": 0,
                    "property_index": {
                        "complete": True,
                        "paths_complete": True,
                        "non_array_complete": True,
                        "indexed_paths": 0,
                        "total_paths": 0,
                        "omitted_values": 0,
                    },
                }
            ],
            "relations": [],
        }
    )
    assert result.nodes[0].created
    with pytest.raises(ValidationError):
        WriteResult.model_validate({"nodes": [], "relations": [], "hidden": 1})


def test_final_search_and_reindex_closed_field_presence():

    assert SearchRequest.model_validate({"kind": "evidence"}).query_mode == "literal"
    for field, value in (("include_evidence", True), ("type", None), ("properties", None)):
        with pytest.raises(ValueError):
            SearchRequest.model_validate({"kind": "evidence", field: value})
    with pytest.raises(ValueError):
        SearchRequest.model_validate({"kind": "nodes", "media_type": None})
    for value in (
        {"kind": "evidence", "all": True, "encoding": "auto"},
        {"kind": "nodes", "ids": []},
        {"kind": "nodes", "all": True, "ids": ["00000000-0000-4000-8000-000000000001"]},
    ):
        with pytest.raises(ValueError):
            ReindexRequest.model_validate(value)


NON_CANONICAL = "550E8400-E29B-41D4-A716-446655440000"
# Every request model that carries a client-supplied graph identifier, with the argument that
# carries it. `validate_record_id` reaches only the three list-valued ones, which is why E4 landed
# on the `RecordID` alias instead: the rest never called it.
INGRESS = [
    (NodeRef, {"id": NON_CANONICAL}),
    (TargetRef, {"kind": "nodes", "id": NON_CANONICAL}),
    (NodeWrite, {"id": NON_CANONICAL}),
    (RelationWrite, {"id": NON_CANONICAL}),
    (WriteRequest, {"nodes": [{"id": NON_CANONICAL}]}),
    (WriteRequest, {"relations": [{"id": NON_CANONICAL}]}),
    (
        WriteRequest,
        {
            "relations": [
                {
                    "type": "resolves_to",
                    "properties": {},
                    "source_ref": {"id": NON_CANONICAL},
                    "target_ref": {"id": str(uuid4())},
                }
            ]
        },
    ),
    (GetRequest, {"kind": "nodes", "ids": [NON_CANONICAL]}),
    (DeleteRequest, {"kind": "nodes", "ids": [NON_CANONICAL]}),
    (ReindexRequest, {"kind": "nodes", "ids": [NON_CANONICAL]}),
    (SearchRequest, {"kind": "relations", "source_id": NON_CANONICAL}),
    (SearchRequest, {"kind": "relations", "target_id": NON_CANONICAL}),
    (NeighborsRequest, {"seed_ids": [NON_CANONICAL]}),
    (JobsRequest, {"action": "get", "job_id": NON_CANONICAL}),
]


@pytest.mark.parametrize(("model", "payload"), INGRESS, ids=lambda value: getattr(value, "__name__", ""))
def test_every_ingress_model_refuses_a_non_canonical_identifier(model, payload):
    """A spelling SQLite cannot match is refused rather than accepted and silently never found."""
    assert UUID(NON_CANONICAL)
    with pytest.raises(ValidationError, match="non-canonical graph id"):
        model.model_validate(payload)


def test_the_ingress_alias_publishes_its_width_and_spelling_to_a_client():
    """Keep the constraints on the alias, where `list_tools()` can publish them, not in a tool body.

    The obvious way to give a signature rejection an application envelope is to loosen the argument
    to plain `str` and check the spelling in the tool function. That works, and silently drops these
    keys from the published input schema, so an MCP host can no longer reject a bad identifier
    before sending it. `tools/request_presence.py` publishes the envelope from the middleware
    instead, which leaves this schema untouched.

    The canonical-spelling rule stays an `AfterValidator`, which has no JSON Schema spelling of its
    own; `json_schema_extra` publishes the grammar that validator applies beside the width, as
    metadata rather than a second check. `tests/tools/test_published_identifier_grammar.py` holds
    the same pattern at every argument `list_tools()` actually reaches.
    """
    assert TypeAdapter(models.RecordID).json_schema() == {
        "type": "string",
        "minLength": 36,
        "maxLength": 36,
        "pattern": GRAPH_ID_PATTERN,
    }


def _record_id_fields(model: type) -> set[str]:
    """Name the fields whose annotation still carries the width-only egress alias."""
    hints = get_type_hints(model, include_extras=True)
    found: set[str] = set()
    for name, hint in hints.items():
        pending, seen = [hint], set()
        while pending:
            current = pending.pop()
            if get_origin(current) is Annotated:
                if get_args(current)[1:] == get_args(StoredRecordID)[1:]:
                    found.add(name)
                continue
            for argument in get_args(current):
                if argument not in seen:
                    seen.add(argument)
                    pending.append(argument)
    return found


def test_no_request_model_carries_the_egress_identifier_alias():
    """The completeness guard for the table above: a new ingress field cannot pick the loose alias.

    `StoredRecordID` bounds width and nothing else, which is correct for a server-generated
    identifier and wrong for anything a client sends. Request models are the closed models reachable
    from the tool wrappers, so every one of them is checked rather than only those listed above.
    """
    requests = [
        model
        for model in (*vars(models).values(), JobsRequest, ReindexRequest)
        if isinstance(model, type) and issubclass(model, ClosedModel) and model.__name__.endswith(("Request", "Ref"))
    ]
    assert {model.__name__ for model in requests} >= {"GetRequest", "JobsRequest", "NodeRef", "TargetRef"}
    assert {model.__name__: _record_id_fields(model) for model in requests if _record_id_fields(model)} == {}


PROPERTY_INDEX = {
    "complete": True,
    "paths_complete": True,
    "non_array_complete": True,
    "indexed_paths": 0,
    "total_paths": 0,
    "omitted_values": 0,
}
DOMAIN: dict[str, Any] = {"type": "domain", "properties": {"value": "example.test"}}
RELATION: dict[str, Any] = {
    "type": "has_subdomain",
    "properties": {},
    "source_ref": {"node_index": 0},
    "target_ref": {"node_index": 1},
}


@pytest.mark.parametrize(
    ("field", "value"),
    [("ownership", "owned"), ("ownership", "rejected"), ("authorization", "in_scope"), ("allowlist_scoped", False)],
)
def test_node_write_accepts_top_level_state(field, value):
    """Inventory state is sent beside `properties`, on creation and on an ID patch alike."""
    assert getattr(NodeWrite.model_validate({**DOMAIN, field: value}), field) == value
    patch = NodeWrite.model_validate({"id": str(uuid4()), field: value})
    assert patch.model_fields_set == {"id", field}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ownership", "trusted"),
        ("authorization", "maybe"),
        ("allowlist_scoped", 1),
        ("allowlist_scoped", "true"),
        ("ownership", None),
        ("authorization", None),
        ("allowlist_scoped", None),
    ],
)
def test_node_write_refuses_state_outside_the_vocabulary(field, value):
    """A state is a closed value; an explicit null makes no claim and is refused like `observed_at`."""
    with pytest.raises(ValidationError):
        NodeWrite.model_validate({**DOMAIN, field: value})


@pytest.mark.parametrize("field", ["ownership", "authorization", "allowlist_scoped"])
def test_relation_write_stays_closed_to_state(field):
    """Relations carry no inventory state, so the field is unknown rather than ignored."""
    value = False if field == "allowlist_scoped" else "owned"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RelationWrite.model_validate({**RELATION, field: value})


@pytest.mark.parametrize("name", ["ownership", "authorization", "allowlist_scoped", "first_seen", "last_seen"])
@pytest.mark.parametrize("model", [NodeWrite, RelationWrite])
def test_reserved_names_are_refused_as_top_level_properties(model, name):
    """A reserved name inside `properties` would shadow server-managed state; the error names the field."""
    base = DOMAIN if model is NodeWrite else RELATION
    with pytest.raises(ValidationError, match=f"use the top-level {name} field"):
        model.model_validate({**base, "properties": {**base["properties"], name: "owned"}})
    with pytest.raises(ValidationError, match=f"use the top-level {name} field"):
        model.model_validate({"id": str(uuid4()), "properties": {name: None}})


@pytest.mark.parametrize("model", [NodeWrite, RelationWrite])
def test_reserved_names_are_free_below_the_top_level(model):
    """Only top-level keys are reserved; scanner output nests these words freely."""
    base = DOMAIN if model is NodeWrite else RELATION
    nested = {"scanner": {"ownership": "x", "first_seen": "2026-01-01T00:00:00Z"}}
    written = model.model_validate({**base, "properties": {**base["properties"], **nested}})
    assert written.properties["scanner"] == nested["scanner"]


def test_search_state_filters_are_node_only():
    """State lives on nodes, so relation and evidence searches refuse the filters rather than ignore them."""
    request = SearchRequest.model_validate({"kind": "nodes", "ownership": "rejected", "authorization": "in_scope"})
    assert (request.ownership, request.authorization) == ("rejected", "in_scope")
    for kind in ("evidence", "relations"):
        for field, value in (("ownership", "owned"), ("authorization", "unknown")):
            with pytest.raises(ValidationError):
                SearchRequest.model_validate({"kind": kind, field: value})
    with pytest.raises(ValidationError):
        SearchRequest.model_validate({"kind": "nodes", "ownership": "trusted"})
    assert SearchRequest.model_validate({"kind": "relations", "last_seen_min": "2026-01-01T00:00:00Z"})


@pytest.mark.parametrize("field", ["first_seen_min", "first_seen_max", "last_seen_min", "last_seen_max"])
def test_seen_bounds_replace_observed_at_on_graph_searches(field):
    """KTD6: graph searches bound first and last seen; evidence keeps its per-source times."""
    assert SearchRequest.model_validate({"kind": "nodes", field: "2026-01-01T00:00:00Z"})
    for invalid in ({"kind": "evidence", field: "2026-01-01T00:00:00Z"}, {"kind": "nodes", field: "yesterday"}):
        with pytest.raises(ValidationError):
            SearchRequest.model_validate(invalid)
    with pytest.raises(ValidationError):
        SearchRequest.model_validate({"kind": "nodes", "observed_at_min": "2026-01-01T00:00:00Z"})


def test_state_response_fields_are_optional_until_storage_fills_them():
    """Current storage output validates unchanged, and a populated state validates too."""
    root = str(uuid4())
    bare = MutationResult.model_validate(
        {
            "id": str(uuid4()),
            "created": True,
            "updated": False,
            "links_added": 0,
            "links_removed": 0,
            "property_index": PROPERTY_INDEX,
        }
    )
    assert (bare.ownership, bare.authorization, bare.rejection_job_id) == (None, None, None)
    rejected = MutationResult.model_validate(
        {
            **bare.model_dump(),
            "ownership": "rejected",
            "authorization": "out_of_scope",
            "rejection_job_id": str(uuid4()),
        }
    )
    assert rejected.ownership == "rejected"
    summary = SearchSummary.model_validate(
        {"id": str(uuid4()), "ownership": "owned", "authorization": "in_scope", "state_root_id": root}
    )
    assert summary.state_root_id == root
    assert SearchSummary.model_validate({"id": str(uuid4())}).ownership is None
    node = NeighborNode.model_validate(
        {
            "id": str(uuid4()),
            "type": "port",
            "ownership": "owned",
            "authorization": "out_of_scope",
            "state_root_id": root,
        }
    )
    assert node.authorization == "out_of_scope"
    assert NeighborNode.model_validate({"id": str(uuid4()), "type": "domain"}).ownership is None
    with pytest.raises(ValidationError):
        MutationResult.model_validate({**bare.model_dump(), "ownership": "trusted"})


def test_neighbor_edges_carry_no_state():
    """Relations hold no inventory state, so an edge refuses the node-only fields."""
    edge = {"id": str(uuid4()), "type": "has_port", "source_id": str(uuid4()), "target_id": str(uuid4())}
    assert NeighborEdge.model_validate(edge)
    with pytest.raises(ValidationError):
        NeighborEdge.model_validate({**edge, "ownership": "owned"})


def test_state_vocabularies_match_the_catalog():
    """The wire literals and the vocabularies `kb_types` publishes are one list."""
    inventory = catalog_manifest()["inventory"]
    assert list(get_args(models.Ownership)) == inventory["ownership"]
    assert list(get_args(models.Authorization)) == inventory["authorization"]
