"""Canonical predicate semantics independently of SQLite."""

import pytest

from justpen_knowledgebase_mcp.models import SearchRequest
from justpen_knowledgebase_mcp.query import MISSING, compile_filter, evaluate, resolve_pointer, strict_scalar_equal


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (403, "403", False),
        (True, 1, False),
        (None, None, True),
        (1, 1.0, True),
        (2**53 + 1, float(2**53), False),
        (0.0, -0.0, True),
    ],
)
def test_strict_equal(left, right, expected):
    assert strict_scalar_equal(left, right) is expected


def test_pointer_container_semantics():
    assert resolve_pointer({"items": {"00": None}}, "/items/00") is None
    assert resolve_pointer({"items": ["yes"]}, "/items/00") is MISSING
    assert resolve_pointer({"a/b": {"~": 1}}, "/a~1b/~0") == 1


@pytest.mark.parametrize(
    ("document", "expected"), [({}, False), ({"x": {}}, True), ({"x": "403"}, True), ({"x": 403}, False)]
)
def test_ne_requires_existing_value(document, expected):
    assert evaluate(document, {"path": "/x", "op": "ne", "value": 403}) is expected


@pytest.mark.parametrize(
    "expression",
    [
        {"path": "/x", "op": "eq", "value": {}},
        {"path": "/x", "op": "exists", "value": 1},
        {"path": "/x", "op": "gt", "value": True},
        {"path": "/x", "op": "eq", "value": 2**63},
        {"path": "/x", "op": "eq", "value": float("inf")},
        {"path": "/x~2", "op": "eq", "value": 1},
        {"path": "/x", "op": "in", "value": "wrong"},
        {"all": [{"path": "/x", "op": "eq", "value": 1}] * 33},
        {"all": [{"all": [{"all": [{"all": [{"all": [{"path": "/x", "op": "eq", "value": 1}]}]}]}]}]},
    ],
)
def test_filter_rejects_invalid_ast(expression):
    with pytest.raises(ValueError):
        SearchRequest(kind="nodes", properties=expression)


def test_filter_valid_depth_predicate_boundary():
    leaf = {"path": "/items/00", "op": "eq", "value": None}
    SearchRequest(kind="nodes", properties={"all": [{"any": [{"all": [{"any": [leaf] * 32}]}]}]})


def test_impossible_depth_is_missing_without_sql_bind_expansion():
    path = "/child" * 17
    expression = {"path": path, "op": "exists", "value": False}
    sql, bindings = compile_filter(expression, "nodes")
    assert sql == "1"
    assert bindings == []
    for op, value in [("eq", 1), ("ne", 1), ("in", [None]), ("gt", 1), ("exists", True)]:
        assert compile_filter({"path": path, "op": op, "value": value}, "nodes") == ("0", [])


@pytest.mark.parametrize("path", ["/child" * 17 + "/bad~2", "/child" * 17 + "/\ud800"])
def test_impossible_depth_still_validates_pointer(path):
    with pytest.raises(ValueError):
        SearchRequest(kind="nodes", properties={"path": path, "op": "exists", "value": False})


@pytest.mark.parametrize("size", [0, 101])
def test_in_list_bounds(size):
    with pytest.raises(ValueError):
        SearchRequest(kind="nodes", properties={"path": "/x", "op": "in", "value": [None] * size})


def test_in_list_boundary_preserves_duplicates():
    values = [1] * 100
    request = SearchRequest(kind="nodes", properties={"path": "/x", "op": "in", "value": values})
    assert request.properties is not None
    assert request.properties["value"] == values
