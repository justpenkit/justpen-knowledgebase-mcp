"""Search and traversal decisions isolated from SQLite/FTS execution."""

import importlib
from unittest.mock import Mock

import pytest

from justpen_knowledgebase_mcp.errors import InvalidParamsError, NotFoundError
from justpen_knowledgebase_mcp.models import NeighborsRequest, SearchRequest
from justpen_knowledgebase_mcp.query import TextQuery
from justpen_knowledgebase_mcp.storage import traversal

from .helpers import NODE, OTHER, cursor, database, owner

search = importlib.import_module("justpen_knowledgebase_mcp.storage.search")
COVERAGE = {"ready": 2, "pending": 0, "failed": 0, "incomplete": 0, "not_applicable": 0}


def test_search_canonical_fallback_resolves_before_advancing(monkeypatch):
    monkeypatch.setattr(search, "coverage", Mock(return_value=COVERAGE))
    db = database(
        cursor(value=(NODE, 1)),
        cursor(rows=[(1, NODE, "domain", "key", "{}", None), (2, OTHER, "domain", "key", "{}", None)]),
        cursor(value='{"name":"miss"}'),
        cursor(value='{"name":"hit"}'),
        cursor(),
    )
    result = search.search(
        db, Mock(), SearchRequest(kind="nodes", properties={"path": "/name", "op": "eq", "value": "hit"})
    )
    assert [item["id"] for item in result["items"]] == [OTHER]
    assert result["canonical_scan_count"] == 2
    assert result["property_filter_mode"] == "canonical_fallback"
    assert not result["has_more"]


def test_search_pages_only_after_returned_match(monkeypatch):
    monkeypatch.setattr(search, "coverage", Mock(return_value=COVERAGE))
    db = database(
        cursor(value=(NODE, 1)),
        cursor(rows=[(1, NODE, "domain", "key", "{}", True), (2, OTHER, "domain", "key", "{}", True)]),
    )
    first = search.search(db, Mock(), SearchRequest(kind="nodes", limit=1))
    assert first["has_more"]
    assert first["cursor"]
    db = database(cursor(value=(NODE, 1)), cursor())
    search.search(db, Mock(), SearchRequest(kind="nodes", limit=1, cursor=first["cursor"]))
    assert db.execute.call_args.args[1] == [1]
    with pytest.raises(InvalidParamsError):
        search.search(database(cursor(value=(OTHER, 1))), Mock(), SearchRequest(kind="nodes", cursor=first["cursor"]))


def test_relevance_best_bounded_results_and_unverified_candidate(monkeypatch):
    monkeypatch.setattr(search, "coverage", Mock(return_value=COVERAGE))
    monkeypatch.setattr(
        search, "compile_text_query", Mock(return_value=TextQuery("x", "literal", ("x",), ('"x"',), 0, 1))
    )
    monkeypatch.setattr(search, "owner_match", Mock(side_effect=[None, {"score": -1.0}, {"score": -2.0}]))
    db = database(
        cursor(value=(NODE, 1)),
        cursor(
            rows=[
                (1, NODE, "domain", "key", "{}", 1),
                (2, OTHER, "domain", "key", "{}", 1),
                (3, NODE, "domain", "key", "{}", 1),
            ]
        ),
        cursor(),
    )
    result = search.search(
        db, Mock(), SearchRequest(kind="nodes", query="x", sort="relevance", include_evidence=True, limit=1)
    )
    assert result["items"][0]["id"] == NODE
    assert result["items"][0]["score"] == -2
    assert result["has_more"]
    assert result["cursor"] is None
    assert db.execute.call_args.args[1][-2:] == ['"x"', '"x"']


def test_builtin_filters_parameterize_all_user_values():
    request = SearchRequest(
        kind="relations",
        type="subdomain_of",
        key="unsafe'",
        source="unsafe'",
        source_id=NODE,
        target_id=OTHER,
        observed_at_min="2026-01-01T00:00:00Z",
        observed_at_max="2026-01-02T00:00:00Z",
    )
    clauses, values = search._builtin_filters(request)
    assert "unsafe'" not in " ".join(clauses)
    assert values.count("unsafe'") == 2
    assert values[-1] > values[-2]
    request = SearchRequest(
        kind="evidence",
        source="source",
        media_type="text/plain",
        index_state="ready",
        byte_size_min=1,
        byte_size_max=10,
        created_at_min="2026-01-01T00:00:00Z",
        created_at_max="2026-01-02T00:00:00Z",
    )
    clauses, values = search._builtin_filters(request)
    assert len(values) == 7
    assert "evidence_sources" in " ".join(clauses)


@pytest.mark.parametrize(
    ("direction", "outgoing", "incoming", "expected"),
    [
        ("both", 2, 7, 2),
        ("both", 7, 2, 2),
        ("both", None, 7, 7),
        ("both", 7, None, 7),
        ("both", None, None, None),
        ("out", 7, 2, 7),
        ("in", 7, 2, 2),
    ],
)
def test_adjacency_returns_lowest_id_across_direction_and_types(direction, outgoing, incoming, expected):
    def row(identifier):
        return None if identifier is None else (identifier, OTHER, "subdomain_of", 1, 2)

    values = [outgoing, incoming] if direction == "both" else [outgoing if direction == "out" else incoming]
    db = database(*(cursor(rows=[] if identifier is None else [row(identifier)]) for identifier in values))
    request = NeighborsRequest(seed_ids=[NODE], relation_types=["subdomain_of"], direction=direction)
    edge = next(traversal._edges(db, 1, request), None)
    assert (edge[0] if edge else None) == expected
    assert db.execute.call_count == len(values)
    assert db.execute.call_args.args[1] == (1, 0, "subdomain_of")


@pytest.mark.parametrize(("max_nodes", "max_edges", "reason"), [(1, 5, "max_nodes"), (5, 1, "max_edges"), (5, 5, None)])
def test_traversal_cycles_limits_and_frontier(monkeypatch, max_nodes, max_edges, reason):
    monkeypatch.setattr(traversal, "row_by_id", Mock(side_effect=[owner(), owner(id=2, uuid=OTHER)]))
    monkeypatch.setattr(traversal, "require_ready", Mock())
    monkeypatch.setattr(
        traversal,
        "_edges",
        Mock(
            side_effect=[
                (edge for edge in [(1, NODE, "subdomain_of", 1, 2), (2, OTHER, "subdomain_of", 2, 1)]),
                (edge for edge in [(1, NODE, "subdomain_of", 1, 2)]),
            ]
        ),
    )
    request = NeighborsRequest(seed_ids=[NODE], depth=2, max_nodes=max_nodes, max_edges=max_edges)
    result = traversal.neighbors(database(), Mock(deadline=float("inf")), request)
    assert result["reason"] == reason
    assert result["truncated"] == (reason is not None)
    assert len(result["nodes"]) == (1 if max_nodes == 1 else 2)
    if reason:
        assert result["frontier"][0] == NODE
    else:
        assert len(result["edges"]) == 2


def test_traversal_missing_seed_and_deadline(monkeypatch):
    monkeypatch.setattr(traversal, "row_by_id", Mock(return_value=None))
    with pytest.raises(NotFoundError):
        traversal.neighbors(database(), Mock(deadline=0), NeighborsRequest(seed_ids=[NODE]))
    monkeypatch.setattr(traversal, "row_by_id", Mock(return_value=owner()))
    assert traversal.neighbors(database(), Mock(deadline=0), NeighborsRequest(seed_ids=[NODE]))["reason"] == "deadline"
