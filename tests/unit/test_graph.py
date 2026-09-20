"""Graph decisions against scripted rows, with derived-index collaborators isolated."""

import json
from unittest.mock import Mock

import pytest

from justpen_knowledgebase_mcp.catalog import catalog_manifest
from justpen_knowledgebase_mcp.errors import ConflictError, InvalidParamsError, NotFoundError, RecordConflictError
from justpen_knowledgebase_mcp.models import GetRequest, NodeRef, NodeWrite, RelationWrite, TypesRequest, WriteRequest
from justpen_knowledgebase_mcp.storage import graph

from .helpers import EVIDENCE, NODE, OTHER, cursor, database, owner


def test_scoped_node_order_covers_every_parent_scoped_catalog_type():
    """A scoped type missing here is not an import or catalog failure: the first write of it raises
    InvalidParamsError("scoped node dependency could not be resolved") at runtime instead."""
    declared = {name for name, definition in catalog_manifest()["nodes"].items() if "scope" in definition["identity"]}
    assert set(graph._SCOPED_NODE_ORDER) == declared
    assert len(graph._SCOPED_NODE_ORDER) == len(declared)


def test_scoped_node_order_places_every_scoped_parent_before_its_child():
    """Membership is not enough: _prepare_node_plans resolves the tuple in order, so a scoped type
    whose parent is itself scoped must come later. Adding a scoped type to another scoped type's
    scope relation sources without reordering fails at runtime, not here, unless this holds."""
    manifest = catalog_manifest()
    scoped = {
        name: definition["identity"]["scope"]
        for name, definition in manifest["nodes"].items()
        if "scope" in definition["identity"]
    }
    for child, scope in scoped.items():
        for source in manifest["relations"][scope["relation"]]["sources"]:
            if source in scoped:
                assert graph._SCOPED_NODE_ORDER.index(source) < graph._SCOPED_NODE_ORDER.index(child), (
                    f"{source} must precede {child} in _SCOPED_NODE_ORDER"
                )


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
        (
            "has_subdomain",
            "domain",
            {"value": "example.com"},
            "subdomain",
            {"value": "a.example.com"},
            True,
        ),
        (
            "has_subdomain",
            "subdomain",
            {"value": "a.example.com"},
            "subdomain",
            {"value": "a.example.com"},
            False,
        ),
        (
            "contains_ip",
            "ip_cidr",
            {"value": "192.0.2.0/24", "version": 4},
            "ip_address",
            {"value": "192.0.2.1", "version": 4},
            True,
        ),
        (
            "contains_ip",
            "domain",
            {"value": "example.com"},
            "ip_address",
            {"value": "192.0.2.1", "version": 4},
            False,
        ),
        (
            "serves_endpoint",
            "service",
            {"name": "unknown"},
            "endpoint",
            {"url": "https://example.com/", "method": "GET"},
            True,
        ),
        ("has_open_port", "ip_address", {}, "port", {}, True),
        ("has_service", "ip_address", {}, "service", {}, False),
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
    persisted = Mock(return_value=(owner(properties='{"value":"example.com","extra":2}'), True))
    monkeypatch.setattr(graph, "_persist", persisted)
    row, created = graph._upsert(
        db, "nodes", NodeWrite(type="domain", properties={"value": "example.com", "extra": 2}), [], set()
    )
    assert created
    assert row["id"] == 1
    assert persisted.call_args.args[4] == {"value": "example.com", "extra": 2}
    monkeypatch.setattr(graph, "row_by_id", Mock(return_value=owner(properties='{"value":"example.com"}')))
    with pytest.raises(ConflictError, match="identity"):
        graph._upsert(db, "nodes", NodeWrite(id=NODE, properties={"value": "other.com"}), [], set())
    with pytest.raises(ConflictError, match="type"):
        graph._upsert(db, "nodes", NodeWrite(id=NODE, type="subdomain", properties={}), [], set())
    persisted.reset_mock()
    with pytest.raises(InvalidParamsError, match="duplicate"):
        graph._upsert(db, "nodes", NodeWrite(id=NODE, properties={}), [], {("nodes", 1)})
    persisted.assert_not_called()
    monkeypatch.setattr(graph, "row_by_id", Mock(return_value=None))
    with pytest.raises(NotFoundError):
        graph._upsert(db, "nodes", NodeWrite(id=NODE, properties={}), [], set())


def test_write_value_error_does_not_expose_validation_payload(monkeypatch):
    monkeypatch.setattr(graph, "_preflight", Mock(side_effect=ValueError("secret payload " * 1000)))
    with pytest.raises(InvalidParamsError, match=r"^invalid graph mutation$"):
        graph.Graph.write(database(), Mock(), WriteRequest(nodes=[NodeWrite(id=NODE)]))


@pytest.mark.parametrize("existing", [False, True])
def test_persist_preserves_metadata_presence_and_refreshes_indexes(monkeypatch, existing):
    db = database()
    readback = owner()
    monkeypatch.setattr(graph, "row_by_id", Mock(return_value=readback))
    properties = Mock()
    text = Mock()
    monkeypatch.setattr(graph, "refresh_properties", properties)
    monkeypatch.setattr(graph.fulltext, "refresh_record_text", text)
    previous = owner(metadata='{"label":"old","source":"original"}') if existing else None
    mutation = NodeWrite(type="domain", properties={"value": "example.com"}, label=None)
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
    mutation = NodeWrite(type="domain", properties={"value": "example.com"})
    plan = graph._PreparedMutation(
        mutation,
        "domain",
        None,
        owner(properties='{"value":"example.com"}'),
        mutation.properties,
    )
    monkeypatch.setattr(graph, "_preflight", Mock(return_value=([plan], [])))
    monkeypatch.setattr(graph, "_persist", Mock(return_value=(owner(properties='{"value":"example.com"}'), True)))
    monkeypatch.setattr(graph, "_links", Mock(return_value=(2, 0)))
    request = WriteRequest(nodes=[mutation])
    result = graph.Graph.write(database(), Mock(), request)
    assert result["nodes"][0]["links_added"] == 2
    assert result["nodes"][0]["created"] is True
    monkeypatch.setattr(graph, "_preflight", Mock(side_effect=ValueError("invalid property")))
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
    row = owner(
        index_state="ready",
        incomplete=0,
        source_id=2,
        target_id=3,
        sha256="a" * 64,
        byte_size=4,
        media_type="text/plain",
        encoding="utf-8",
    )
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


def test_types_exposes_scoped_identity_in_manifest_and_schema():
    db = database(cursor(value=(NODE, 1)), cursor(value=0))
    result = graph.graph_types(db, Mock(deadline=float("inf")), TypesRequest(kind="nodes", type="port"))
    identity = {
        "properties": ["transport", "number"],
        "scope": {"relation": "has_open_port", "endpoint": "source"},
    }
    assert result["types"][0]["identity"] == identity
    assert result["types"][0]["properties_schema"]["x-identity"] == identity


def test_relation_references_batch_and_existing_endpoints_are_immutable(monkeypatch):

    source = owner(type="domain", properties='{"value":"example.com"}')
    target = owner(id=2, uuid=OTHER, type="ip_address", properties='{"value":"192.0.2.1","version":4}')
    assert graph._ref(database(), NodeRef(node_index=0), [source]) is source
    with pytest.raises(InvalidParamsError, match="outside batch"):
        graph._ref(database(), NodeRef(node_index=1), [source])
    lookup = Mock(return_value=target)
    monkeypatch.setattr(graph, "row_by_id", lookup)
    relation = RelationWrite(
        type="resolves_to", properties={}, source_ref=NodeRef(node_index=0), target_ref=NodeRef(id=OTHER)
    )
    assert graph._endpoints(database(), relation, [source], None, "resolves_to") == (source, target)
    with pytest.raises(ConflictError, match="immutable"):
        graph._endpoints(database(), relation, [source], owner(source_id=3, target_id=2), "resolves_to")
    lookup.side_effect = [source, target]
    patch = RelationWrite(id=NODE)
    assert graph._endpoints(database(), patch, [], owner(source_id=1, target_id=2), "resolves_to") == (
        source,
        target,
    )
    lookup.side_effect = [None]
    with pytest.raises(NotFoundError):
        graph._ref(database(), NodeRef(id=OTHER), [])


def test_dedup_uses_canonical_identity_to_reject_hash_collision(monkeypatch):
    mutation = NodeWrite(type="domain", properties={"value": "example.com"})
    lookup = Mock(return_value=owner(properties='{"value":"example.com"}'))
    monkeypatch.setattr(graph, "row_by_id", lookup)
    assert graph._deduplicate(database(cursor(value=1)), "nodes", mutation, None, "domain", (None, None)) == owner(
        properties='{"value":"example.com"}'
    )
    lookup.return_value = owner(properties='{"value":"different.com"}')
    with pytest.raises(ConflictError, match="collision"):
        graph._deduplicate(database(cursor(value=1)), "nodes", mutation, None, "domain", (None, None))
    lookup.return_value = None
    with pytest.raises(NotFoundError):
        graph._deduplicate(database(cursor(value=1)), "nodes", mutation, None, "domain", (None, None))


def test_relation_persistence_binds_endpoint_ids_and_observation(monkeypatch):

    source, target = owner(), owner(id=2)
    mutation = RelationWrite(
        type="resolves_to",
        properties={},
        source_ref=NodeRef(id=NODE),
        target_ref=NodeRef(id=OTHER),
        observed_at="2026-01-01T00:00:00Z",
    )
    monkeypatch.setattr(graph, "row_by_id", Mock(return_value=owner(type="resolves_to")))
    monkeypatch.setattr(graph, "refresh_properties", Mock())
    monkeypatch.setattr(graph.fulltext, "refresh_record_text", Mock())
    db = database()
    _row, created = graph._persist(db, "relations", mutation, None, {}, (source, target))
    assert created
    values = db.execute.call_args.args[1]
    assert values[1:4] == (1, "resolves_to", 2)
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


@pytest.mark.parametrize("view", ["sources", "links"])
def test_pending_association_owner_returns_authoritative_blocker(monkeypatch, view):
    row = owner(uuid=EVIDENCE, lifecycle="delete_pending", delete_job_id=OTHER, delete_requested_at=3)
    monkeypatch.setattr(graph, "row_by_id", Mock(return_value=row))
    with pytest.raises(RecordConflictError) as raised:
        graph.Graph.get(
            database(cursor(value=(NODE, 1)), cursor(), cursor()),
            Mock(),
            GetRequest(kind="evidence", ids=[EVIDENCE], view=view),
        )
    assert str(raised.value.details.delete_job_id) == OTHER
    assert raised.value.details.blocking_record.kind == "evidence"


@pytest.mark.parametrize(
    ("state", "incomplete"), [("pending", 1), ("ready", 0), ("not_applicable", 0), ("index_failed", 1)]
)
def test_evidence_record_projects_current_bounded_coverage(state, incomplete):
    row = owner(
        uuid=EVIDENCE,
        sha256="a" * 64,
        byte_size=8,
        media_type="text/plain",
        encoding="utf-8",
        index_state=state,
        incomplete=incomplete,
        index_owner_token=OTHER,
    )
    db = database()
    db.execute.return_value.get = 0
    record = graph._record(db, "evidence", row)
    assert record["index_state"] == state
    assert record["incomplete"] is bool(incomplete)
    assert "index_owner_token" not in record


def test_catalog_validation_preserves_authored_field_rule():
    request = WriteRequest(nodes=[NodeWrite(type="domain", properties={"value": "SECRET-MARKER"})])
    with pytest.raises(InvalidParamsError, match="/properties/value: expected dns_name"):
        graph.Graph.write(database(cursor(value=None)), Mock(), request)


def test_mutation_validation_preserves_authored_conflict(monkeypatch):
    monkeypatch.setattr(graph, "row_by_id", Mock(return_value=owner(properties='{"value":"example.com","a":{}}')))
    request = WriteRequest(nodes=[NodeWrite(id=NODE, properties={"a": {}}, remove_properties=["/a"])])
    with pytest.raises(InvalidParamsError, match="remove and set paths conflict"):
        graph.Graph.write(database(), Mock(), request)
