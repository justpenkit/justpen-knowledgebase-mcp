"""Closed strict request models preserve nested raw field presence."""

from typing import Annotated, get_args, get_origin, get_type_hints
from uuid import UUID, uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from justpen_knowledgebase_mcp import models
from justpen_knowledgebase_mcp.jobs import JobsRequest
from justpen_knowledgebase_mcp.models import (
    ClosedModel,
    DeleteRequest,
    GetRequest,
    NeighborsRequest,
    NodeRef,
    NodeWrite,
    RelationWrite,
    SearchRequest,
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


def test_the_ingress_alias_publishes_its_width_to_a_client():
    """Keep the width on the alias, where `list_tools()` can publish it, not inside a tool body.

    The obvious way to give a signature rejection an application envelope is to loosen the argument
    to plain `str` and check the spelling in the tool function. That works, and silently drops these
    two keys from the published input schema, so an MCP host can no longer reject an over-long
    identifier before sending it. `tools/request_presence.py` publishes the envelope from the
    middleware instead, which leaves this schema untouched.

    The canonical-spelling rule itself is an `AfterValidator` with no JSON Schema spelling, so a
    client cannot predict that refusal from the schema alone; only the width is promised.
    """
    assert TypeAdapter(models.RecordID).json_schema() == {"type": "string", "minLength": 36, "maxLength": 36}


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
