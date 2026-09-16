"""Projection coverage and typed evidence contract."""

from typing import Any

from justpen_knowledgebase_mcp.indexing import flatten_properties
from justpen_knowledgebase_mcp.query import evaluate, index_evidence


def test_priority_and_scalar_budget():
    projection = flatten_properties(
        {"items": list(range(600)), "zzz_status_code": 403, "zzz_id": "id"}, required_paths={"/zzz_id"}
    )
    assert len(projection.rows) == 512
    assert projection.rows["/zzz_status_code"].value == 403
    assert projection.non_array_complete
    assert not projection.paths_complete
    assert projection.total_paths == 603


def test_bytes_and_numeric_object_keys():
    projection = flatten_properties({"0": "x" * 1025, "a": "é" * 512, "b": "é" * 513, "x" * 1024: 1})
    assert projection.rows["/0"].value_materialized is False
    assert projection.rows["/a"].value_materialized is True
    assert projection.omitted_values == 2
    assert not projection.non_array_complete


def test_required_and_object_numeric_paths_have_priority():
    properties: dict[str, Any] = {f"a{index:04}": index for index in range(600)}
    properties.update(zzz_id="required", obj={"0": "value"}, ports=list(range(600)))
    projection = flatten_properties(properties, required_paths={"/zzz_id"})
    assert "/zzz_id" in projection.rows
    assert "/a0510" in projection.rows
    assert "/obj/0" not in projection.rows
    assert not projection.non_array_complete
    numeric = flatten_properties({"obj": {"0": 1}, "array": [2]})
    assert numeric.rows["/obj/0"].value == 1
    assert numeric.rows["/array/0"].value == 2


def test_index_evidence_matches_canonical_for_known_results():
    properties = {"values": list(range(600)), "large": "é" * 513, "scalar": None, "object": {"00": 1}}
    projection = flatten_properties(properties)
    for path in ["/values/599", "/values/00", "/large", "/scalar/child", "/object/00", "/missing"]:
        for op, value in [
            ("exists", False),
            ("exists", True),
            ("eq", "é" * 513),
            ("ne", 2),
            ("gt", 2),
            ("in", [1, None]),
        ]:
            expression = {"path": path, "op": op, "value": value}
            answer = index_evidence(projection, expression)
            if answer is not None:
                assert answer is evaluate(properties, expression)
    assert (
        index_evidence(
            projection,
            {"all": [{"path": "/large", "op": "eq", "value": "é" * 513}, {"path": "/scalar", "op": "eq", "value": 1}]},
        )
        is False
    )
    assert (
        index_evidence(
            projection,
            {
                "any": [
                    {"path": "/large", "op": "eq", "value": "é" * 513},
                    {"path": "/scalar", "op": "eq", "value": None},
                ]
            },
        )
        is True
    )
