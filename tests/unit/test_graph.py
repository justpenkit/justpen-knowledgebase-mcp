"""Graph decisions against scripted rows, with derived-index collaborators isolated."""

import json
from unittest.mock import Mock

import pytest

from justpen_knowledgebase_mcp.errors import ConflictError, InvalidParamsError, NotFoundError, RecordConflictError
from justpen_knowledgebase_mcp.models import GetRequest, NodeRef, NodeWrite, RelationWrite, TypesRequest, WriteRequest
from justpen_knowledgebase_mcp.storage import graph

from .helpers import EVIDENCE, NODE, OTHER, cursor, database, owner


def test_row_materialization_and_kind_validation():
    db = database(cursor(record=owner()), cursor())
    assert graph.row_by_id(db, "nodes", 1) == owner()
    assert graph.row_by_id(db, "nodes", NODE) is None
    assert db.execute.call_args_list[0].args[1] == (1,)
    with pytest.raises(InvalidParamsError):
        graph.row_by_id(db, "attacker", NODE)


def test_pending_relation_precedence_and_missing_endpoint(monkeypatch):
    pending = owner(lifecycle="delete_pending", delete_job_id=OTHER, delete_requested_at=10)
    lookup = Mock(side_effect=[pending])
    monkeypatch.setattr(graph, "row_by_id", lookup)
    relation = owner(source_id=1, target_id=2)
    with pytest.raises(RecordConflictError, match="RECORD_DELETING"):
        graph.require_ready(database(), "relations", relation)
    assert lookup.call_count == 1
    lookup.side_effect = [None]
    with pytest.raises(NotFoundError):
        graph.pending_blocker(database(), "relations", relation)
    lookup.side_effect = [owner(), owner()]
    assert graph.pending_blocker(database(), "relations", relation) is None
    assert graph.pending_blocker(database(), "nodes", owner()) is None


@pytest.mark.parametrize(
    ("relation", "source_type", "source", "target_type", "target", "valid"),
    [
        ("name_in_domain", "hostname", {"name": "a.example.com"}, "domain", {"name": "example.com"}, True),
        ("subdomain_of", "domain", {"name": "example.com"}, "domain", {"name": "example.com"}, False),
        ("offers_service", "ip", {"address": "127.0.0.1"}, "service", {"host": "127.0.0.1"}, True),
        ("offers_service", "hostname", {"name": "a.example.com"}, "service", {"host": "other.com"}, False),
        (
            "serves_endpoint",
            "service",
            {"host": "127.0.0.1", "transport": "tcp", "port": 443},
            "endpoint",
            {"url": "https://example.com/"},
            True,
        ),
        (
            "serves_endpoint",
            "service",
            {"host": "wrong.com", "transport": "tcp", "port": 80},
            "endpoint",
            {"url": "http://example.com/"},
            False,
        ),
        ("member_of", "principal", {"realm": "a"}, "principal", {"realm": "a", "kind": "group"}, True),
    ],
)
def test_endpoint_constraints(relation, source_type, source, target_type, target, valid):
    first = owner(type=source_type, properties=json.dumps(source))
    second = owner(id=2, type=target_type, properties=json.dumps(target))
    if valid:
        graph._validate_endpoints(relation, first, second)
    else:
        with pytest.raises(InvalidParamsError):
            graph._validate_endpoints(relation, first, second)


def test_upsert_merge_identity_and_duplicate_boundaries(monkeypatch):
    db = database(cursor(value=None))
    persisted = Mock(return_value=(owner(properties='{"name":"example.com","extra":2}'), True))
    monkeypatch.setattr(graph, "_persist", persisted)
    row, created = graph._upsert(
        db, "nodes", NodeWrite(type="domain", properties={"name": "example.com", "extra": 2}), [], set()
    )
    assert created
    assert row["id"] == 1
    assert persisted.call_args.args[4] == {"name": "example.com", "extra": 2}
    monkeypatch.setattr(graph, "row_by_id", Mock(return_value=owner()))
    with pytest.raises(ConflictError, match="identity"):
        graph._upsert(db, "nodes", NodeWrite(id=NODE, properties={"name": "other.com"}), [], set())
    with pytest.raises(ConflictError, match="type"):
        graph._upsert(db, "nodes", NodeWrite(id=NODE, type="hostname", properties={}), [], set())
    with pytest.raises(InvalidParamsError, match="duplicate"):
        graph._upsert(db, "nodes", NodeWrite(id=NODE, properties={}), [], {("nodes", 1)})
    monkeypatch.setattr(graph, "row_by_id", Mock(return_value=None))
    with pytest.raises(NotFoundError):
        graph._upsert(db, "nodes", NodeWrite(id=NODE, properties={}), [], set())


@pytest.mark.parametrize("existing", [False, True])
def test_persist_preserves_metadata_presence_and_refreshes_indexes(monkeypatch, existing):
    db = database()
    readback = owner()
    monkeypatch.setattr(graph, "row_by_id", Mock(return_value=readback))
    properties = Mock()
    text = Mock()
    monkeypatch.setattr(graph, "refresh_properties", properties)
    monkeypatch.setattr(graph, "refresh_record_text", text)
    previous = owner(metadata='{"label":"old","source":"original"}') if existing else None
    mutation = NodeWrite(type="domain", properties={"name": "example.com"}, label=None)
    result, created = graph._persist(db, "nodes", mutation, previous, mutation.properties, (None, None))
    assert result is readback
    assert created != existing
    parameters = db.execute.call_args.args[1]
    metadata = json.loads(parameters[1] if existing else parameters[4])
    assert metadata == {"label": None, "source": "original" if existing else None}
    properties.assert_called_once_with(db, "nodes", readback, mutation.properties)
    text.assert_called_once_with(db, "nodes", readback)


def test_links_counts_actual_changes_and_rejects_missing(monkeypatch):
    lookup = Mock(side_effect=[owner(uuid=EVIDENCE), owner(uuid="e_" + "b" * 64)])
    monkeypatch.setattr(graph, "row_by_id", lookup)
    db = database()
    db.changes.side_effect = [0, 1]
    mutation = NodeWrite(id=NODE, evidence_add=[EVIDENCE], evidence_remove=["e_" + "b" * 64])
    assert graph._links(db, "nodes", owner(), mutation) == (0, 1)
    lookup.side_effect = [None]
    with pytest.raises(NotFoundError):
        graph._links(db, "nodes", owner(), mutation)


def test_write_materializes_results_and_maps_validation(monkeypatch):
    monkeypatch.setattr(graph, "_upsert", Mock(return_value=(owner(), True)))
    monkeypatch.setattr(graph, "_links", Mock(return_value=(2, 0)))
    request = WriteRequest(nodes=[NodeWrite(type="domain", properties={"name": "example.com"})])
    result = graph.Graph.write(database(), Mock(), request)
    assert result["nodes"][0]["links_added"] == 2
    assert result["nodes"][0]["created"] is True
    monkeypatch.setattr(graph, "_upsert", Mock(side_effect=ValueError("invalid property")))
    with pytest.raises(InvalidParamsError):
        graph.Graph.write(database(), Mock(), request)


def test_get_missing_and_bounded_whole_records(monkeypatch):
    monkeypatch.setattr(graph, "row_by_id", Mock(side_effect=[owner(), None, owner(uuid=OTHER)]))
    monkeypatch.setattr(graph, "_record", Mock(side_effect=[{"id": NODE}, {"id": OTHER, "properties": "x" * 250000}]))
    result = graph.Graph.get(
        database(), Mock(), GetRequest(kind="nodes", ids=[NODE, "00000000-0000-4000-8000-000000000003", OTHER])
    )
    assert result == {
        "records": [{"id": NODE}],
        "missing_ids": ["00000000-0000-4000-8000-000000000003"],
        "remaining_ids": [OTHER],
    }


@pytest.mark.parametrize("kind", ["nodes", "relations", "evidence"])
def test_record_projection_and_effective_lifecycle(monkeypatch, kind):
    db = database()
    db.execute.return_value.get = 3
    monkeypatch.setattr(graph, "pending_blocker", Mock(return_value=None))
    row = owner(source_id=2, target_id=3, sha256="a" * 64, byte_size=4, media_type="text/plain", encoding="utf-8")
    result = graph._record(db, kind, row)
    assert result["lifecycle"] == "ready"
    assert result["link_count"] == (6 if kind == "evidence" else 3)
    assert result["id"] == NODE


def test_association_cursor_tie_break_and_limits(monkeypatch):
    monkeypatch.setattr(graph, "row_by_id", Mock(return_value=owner(uuid=EVIDENCE)))
    db = database(cursor(value=(NODE, 1)), cursor(rows=[(2, NODE)]), cursor(rows=[(2, OTHER)]))
    first = graph.Graph.get(db, Mock(), GetRequest(kind="evidence", ids=[EVIDENCE], view="links", limit=1))
    assert first["links"] == [{"kind": "nodes", "id": NODE}]
    assert first["next_cursor"]
    db = database(cursor(value=(NODE, 1)), cursor(rows=[]), cursor(rows=[(2, OTHER)]))
    second = graph.Graph.get(
        db, Mock(), GetRequest(kind="evidence", ids=[EVIDENCE], view="links", limit=1, cursor=first["next_cursor"])
    )
    assert second == {"links": [{"kind": "relations", "id": OTHER}], "next_cursor": None}
    assert db.execute.call_args_list[-1].args[1][1] == 1


@pytest.mark.parametrize("deadline", [0, float("inf")])
def test_types_defer_counts_without_losing_schema(deadline):
    db = database(cursor(value=(NODE, 1)), cursor(value=4))
    result = graph.graph_types(db, Mock(deadline=deadline), TypesRequest(kind="nodes", type="domain"))
    assert result["types"][0]["type"] == "domain"
    assert result["types"][0]["count"] == (None if deadline == 0 else 4)
    assert result["counts_deferred"] == (deadline == 0)


def test_relation_references_batch_and_existing_endpoints_are_immutable(monkeypatch):

    source = owner(type="hostname", properties='{"name":"a.example.com"}')
    target = owner(id=2, uuid=OTHER)
    assert graph._ref(database(), NodeRef(node_index=0), [source]) is source
    with pytest.raises(InvalidParamsError, match="outside batch"):
        graph._ref(database(), NodeRef(node_index=1), [source])
    lookup = Mock(return_value=target)
    monkeypatch.setattr(graph, "row_by_id", lookup)
    relation = RelationWrite(
        type="name_in_domain", properties={}, source_ref=NodeRef(node_index=0), target_ref=NodeRef(id=OTHER)
    )
    assert graph._endpoints(database(), relation, [source], None, "name_in_domain") == (source, target)
    with pytest.raises(ConflictError, match="immutable"):
        graph._endpoints(database(), relation, [source], owner(source_id=3, target_id=2), "name_in_domain")
    lookup.side_effect = [source, target]
    patch = RelationWrite(id=NODE)
    assert graph._endpoints(database(), patch, [], owner(source_id=1, target_id=2), "name_in_domain") == (
        source,
        target,
    )
    lookup.side_effect = [None]
    with pytest.raises(NotFoundError):
        graph._ref(database(), NodeRef(id=OTHER), [])


def test_dedup_uses_canonical_identity_to_reject_hash_collision(monkeypatch):
    mutation = NodeWrite(type="domain", properties={"name": "example.com"})
    lookup = Mock(return_value=owner())
    monkeypatch.setattr(graph, "row_by_id", lookup)
    assert graph._deduplicate(database(cursor(value=1)), "nodes", mutation, None, "domain", (None, None)) == owner()
    lookup.return_value = owner(properties='{"name":"different.com"}')
    with pytest.raises(ConflictError, match="collision"):
        graph._deduplicate(database(cursor(value=1)), "nodes", mutation, None, "domain", (None, None))
    lookup.return_value = None
    with pytest.raises(NotFoundError):
        graph._deduplicate(database(cursor(value=1)), "nodes", mutation, None, "domain", (None, None))


def test_relation_persistence_binds_endpoint_ids_and_observation(monkeypatch):

    source, target = owner(), owner(id=2)
    mutation = RelationWrite(
        type="subdomain_of",
        properties={},
        source_ref=NodeRef(id=NODE),
        target_ref=NodeRef(id=OTHER),
        observed_at="2026-01-01T00:00:00Z",
    )
    monkeypatch.setattr(graph, "row_by_id", Mock(return_value=owner(type="subdomain_of")))
    monkeypatch.setattr(graph, "refresh_properties", Mock())
    monkeypatch.setattr(graph, "refresh_record_text", Mock())
    db = database()
    _row, created = graph._persist(db, "relations", mutation, None, {}, (source, target))
    assert created
    values = db.execute.call_args.args[1]
    assert values[1:4] == (1, "subdomain_of", 2)
    assert values[-1] == graph.parse_timestamp("2026-01-01T00:00:00Z")


def test_sources_association_invalid_cursor_and_missing_owner(monkeypatch):
    lookup = Mock(return_value=owner(uuid=EVIDENCE))
    monkeypatch.setattr(graph, "row_by_id", lookup)
    db = database(cursor(value=(NODE, 1)), cursor(rows=[(1, "probe", "first", "last")]))
    result = graph.Graph.get(db, Mock(), GetRequest(kind="evidence", ids=[EVIDENCE], view="sources"))
    assert result == {
        "sources": [{"source": "probe", "first_seen_at": "first", "last_seen_at": "last"}],
        "next_cursor": None,
    }
    with pytest.raises(InvalidParamsError, match="cursor"):
        graph.Graph.get(
            database(cursor(value=(NODE, 1))),
            Mock(),
            GetRequest(kind="evidence", ids=[EVIDENCE], view="sources", cursor="broken"),
        )
    lookup.return_value = None
    with pytest.raises(NotFoundError):
        graph.Graph.get(database(), Mock(), GetRequest(kind="evidence", ids=[EVIDENCE], view="sources"))
