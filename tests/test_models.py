"""Closed strict request models preserve nested raw field presence."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from justpen_knowledgebase_mcp.models import (
    DeleteRequest,
    GetRequest,
    NodeRef,
    NodeWrite,
    SearchRequest,
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
        NodeWrite.model_validate({"type": "ip", "properties": {"address": "192.0.2.1"}, field: "x"})


def test_nested_presence_and_null_survive():
    model = WriteRequest.model_validate(
        {"nodes": [{"type": "ip", "properties": {"address": "192.0.2.1", "extra": None}, "label": None}]}
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
