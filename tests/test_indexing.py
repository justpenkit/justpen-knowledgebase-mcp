"""Projection coverage and typed evidence contract."""

import json
from typing import Any

import apsw

from justpen_knowledgebase_mcp.indexing import flatten_properties
from justpen_knowledgebase_mcp.query import compile_filter, evaluate
from justpen_knowledgebase_mcp.storage.graph_sql import PROPERTY_INSERT
from justpen_knowledgebase_mcp.storage.schema import _property_ddl


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


_EVIDENCE_SELECT = "SELECT {expression} FROM nodes o WHERE o.id=1"


def _projected(properties: dict[str, Any]) -> apsw.Connection:
    """Build the one owner row and property projection the compiled SQL reads."""
    connection = apsw.Connection(":memory:")
    connection.execute("CREATE TABLE nodes(id INTEGER PRIMARY KEY, metadata TEXT);" + _property_ddl("node"))
    projection = flatten_properties(properties)
    connection.execute(
        "INSERT INTO nodes(id,metadata) VALUES(1,?)", (json.dumps({"property_index": projection.metadata()}),)
    )
    connection.executemany(
        PROPERTY_INSERT["nodes"],
        [(1, path, row.value_type, int(row.value_materialized), row.value) for path, row in projection.rows.items()],
    )
    return connection


def _sql_evidence(connection: apsw.Connection, expression: dict[str, Any]) -> bool | None:
    compiled, parameters = compile_filter(expression, "nodes")
    # Server-authored SQL around bound user literals, exactly as `storage/search.py` composes it.
    statement = _EVIDENCE_SELECT.format(expression=compiled)
    answer = connection.execute(statement, parameters).get
    return None if answer is None else bool(answer)


def test_sql_index_evidence_matches_canonical_for_known_results():
    """The live index evidence is the compiled SQL plus the canonical `evaluate` fallback that
    `storage/search.py` runs when the SQL answer is NULL. Every decided answer must agree with the
    canonical one; an undecided answer is a licence to read the body, never a wrong result."""
    properties: dict[str, Any] = {
        "values": list(range(600)),
        "large": "\u00e9" * 513,
        "scalar": None,
        "object": {"00": 1},
    }
    connection = _projected(properties)
    for path in ["/values/599", "/values/00", "/large", "/scalar/child", "/object/00", "/missing"]:
        for op, value in [
            ("exists", False),
            ("exists", True),
            ("eq", "\u00e9" * 513),
            ("ne", 2),
            ("gt", 2),
            ("in", [1, None]),
        ]:
            expression = {"path": path, "op": op, "value": value}
            answer = _sql_evidence(connection, expression)
            if answer is not None:
                assert answer is evaluate(properties, expression), expression
    # An unmaterialized value leaves its own leaf undecided, and the group still decides: a false
    # sibling settles `all`, a true sibling settles `any`.
    assert (
        _sql_evidence(
            connection,
            {
                "all": [
                    {"path": "/large", "op": "eq", "value": "\u00e9" * 513},
                    {"path": "/scalar", "op": "eq", "value": 1},
                ]
            },
        )
        is False
    )
    assert _sql_evidence(connection, {"path": "/large", "op": "eq", "value": "\u00e9" * 513}) is None
    assert (
        _sql_evidence(
            connection,
            {
                "any": [
                    {"path": "/large", "op": "eq", "value": "\u00e9" * 513},
                    {"path": "/scalar", "op": "eq", "value": None},
                ]
            },
        )
        is True
    )
