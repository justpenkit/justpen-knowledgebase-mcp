"""Search and traversal decisions isolated from SQLite/FTS execution."""

import importlib
import json
from unittest.mock import Mock

import pytest

from justpen_knowledgebase_mcp.errors import InvalidParamsError, NotFoundError
from justpen_knowledgebase_mcp.models import NeighborsRequest, SearchRequest
from justpen_knowledgebase_mcp.mutations import canonical_json
from justpen_knowledgebase_mcp.query import TextQuery
from justpen_knowledgebase_mcp.storage import graph, traversal

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
        type="has_subdomain",
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
    ("kind", "include_evidence", "match_count"),
    [("nodes", False, 2), ("nodes", True, 4), ("relations", True, 4), ("evidence", True, 2)],
)
def test_words_candidate_expressions_are_bound_for_each_match_unit(kind, include_evidence, match_count):
    expressions = ('"common"', '"rare unsafe\'"')
    sql, values = search._text_candidates(kind, expressions, include_evidence=include_evidence)
    assert sql.count("MATCH ?") == match_count
    assert values == list(expressions) * (2 if kind != "evidence" and include_evidence else 1)
    assert "unsafe" not in sql


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
        return None if identifier is None else (identifier, OTHER, "has_subdomain", 1, 2)

    values = [outgoing, incoming] if direction == "both" else [outgoing if direction == "out" else incoming]
    db = database(*(cursor(rows=[] if identifier is None else [row(identifier)]) for identifier in values))
    request = NeighborsRequest(seed_ids=[NODE], relation_types=["has_subdomain"], direction=direction)
    edge = next(traversal._edges(db, 1, request), None)
    assert (edge[0] if edge else None) == expected
    assert db.execute.call_count == len(values)
    assert db.execute.call_args.args[1] == (1, 0, "has_subdomain")


@pytest.mark.parametrize(("max_nodes", "max_edges", "reason"), [(1, 5, "max_nodes"), (5, 1, "max_edges"), (5, 5, None)])
def test_traversal_cycles_limits_and_frontier(monkeypatch, max_nodes, max_edges, reason):
    monkeypatch.setattr(traversal, "row_by_id", Mock(side_effect=[owner(), owner(id=2, uuid=OTHER)]))
    monkeypatch.setattr(traversal, "require_ready", Mock())
    monkeypatch.setattr(
        traversal,
        "_edges",
        Mock(
            side_effect=[
                (edge for edge in [(1, NODE, "has_subdomain", 1, 2), (2, OTHER, "has_subdomain", 2, 1)]),
                (edge for edge in [(1, NODE, "has_subdomain", 1, 2)]),
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


def serialization_counter(monkeypatch, module):
    """Count bytes handed to canonical_json so cost is read as work, not as elapsed time."""
    total = [0]

    def counted(value):
        text = canonical_json(value)
        total[0] += len(text.encode("utf-8"))
        return text

    monkeypatch.setattr(module, "canonical_json", counted)
    return lambda: total[0]


def test_search_budget_serializes_each_returned_item_once(monkeypatch):
    """P5: the budget re-serialized the whole output, items included, for every candidate, and
    copied the accumulated item list to do it. Serialized volume is the observable that separates
    the running counter from that shape; the call count is identical under both."""
    monkeypatch.setattr(search, "coverage", Mock(return_value=COVERAGE))
    metadata = json.dumps({"label": "l" * 1024, "source": None})
    rows = [(index, NODE, "domain", "key", metadata, True) for index in range(1, 101)]
    db = database(cursor(value=(NODE, 1)), cursor(rows=rows))
    serialized = serialization_counter(monkeypatch, search)
    result = search.search(db, Mock(), SearchRequest(kind="nodes", limit=100))
    assert len(result["items"]) == 100
    # Each returned item is serialized once for its own cost plus a small fixed envelope per item;
    # the replaced shape serialized the whole accumulated output one hundred times.
    volume, response = serialized(), len(canonical_json(result).encode("utf-8"))
    assert volume <= 4 * response


@pytest.mark.parametrize("module", [search, traversal, graph])
def test_member_accounting_over_counts_each_array_by_exactly_one_separator(module):
    """The whole budget change rests on this identity: an empty array in the envelope plus one
    charge per member is the filled output plus exactly one byte. One byte over per array is safe
    because it stops the page earlier; any byte under would let a page exceed its declared budget."""
    members = [{"id": NODE, "type": "domain"}, {"id": OTHER, "type": "subdomain"}]
    envelope = len(canonical_json({"items": [], "truncated": False}).encode("utf-8"))
    filled = len(canonical_json({"items": members, "truncated": False}).encode("utf-8"))
    accounted = sum(module._member_bytes(member) for member in members)
    assert envelope + accounted == filled + 1


def test_search_page_breaks_within_one_item_of_the_declared_budget(monkeypatch):
    """The moved boundary, asserted rather than absorbed: the page still fills the budget to within
    the one item it refused plus the separator the accounting adds."""
    monkeypatch.setattr(search, "coverage", Mock(return_value=COVERAGE))
    metadata = json.dumps({"label": "l" * 16384, "source": None})
    rows = [(index, NODE, "domain", "key", metadata, True) for index in range(1, 101)]
    result = search.search(
        database(cursor(value=(NODE, 1)), cursor(rows=rows)), Mock(), SearchRequest(kind="nodes", limit=100)
    )
    assert result["has_more"]
    accounted = len(canonical_json({**result, "cursor": None}).encode("utf-8"))
    refused = search._member_bytes(result["items"][0]) + 1
    assert search.RESPONSE_BYTES - refused <= accounted <= search.RESPONSE_BYTES
