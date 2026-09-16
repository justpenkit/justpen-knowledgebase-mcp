"""Delete admission and bounded cleanup decisions with isolated database responses."""

from unittest.mock import Mock

import pytest

from justpen_knowledgebase_mcp.errors import ConflictError, InvalidParamsError, MissingRecordsError, RecordConflictError
from justpen_knowledgebase_mcp.models import DeleteRequest
from justpen_knowledgebase_mcp.storage import deletions

from .helpers import EVIDENCE, NODE, OTHER, cursor, database, owner


def test_prepare_checks_entire_batch_before_any_mutation(monkeypatch):
    lookup = Mock(side_effect=[owner(), None])
    monkeypatch.setattr(deletions, "row_by_id", lookup)
    db = database()
    with pytest.raises(MissingRecordsError):
        deletions.GraphDeletion.prepare(db, DeleteRequest(kind="nodes", ids=[NODE, OTHER], cascade=True), NODE)
    db.execute.assert_not_called()
    lookup.side_effect = [owner(), owner(id=2, uuid=OTHER)]
    ready = Mock(side_effect=[None, ConflictError("pending")])
    monkeypatch.setattr(deletions, "require_ready", ready)
    with pytest.raises(ConflictError):
        deletions.GraphDeletion.prepare(db, DeleteRequest(kind="nodes", ids=[NODE, OTHER], cascade=True), NODE)
    db.execute.assert_not_called()
    lookup.side_effect = [owner(), owner(id=2, uuid=OTHER)]
    ready.side_effect = None
    intents = deletions.GraphDeletion.prepare(db, DeleteRequest(kind="nodes", ids=[NODE, OTHER], cascade=True), NODE)
    assert [intent.uuid for intent in intents] == [NODE, OTHER]
    assert intents[0].requested_at == intents[1].requested_at
    assert db.execute.call_count == 2


@pytest.mark.parametrize("kind", ["nodes", "relations", "evidence"])
def test_dependencies_select_first_owner_and_surface_blocker(monkeypatch, kind):
    monkeypatch.setattr(
        deletions, "row_by_id", Mock(return_value=owner(uuid=EVIDENCE if kind == "relations" else OTHER))
    )
    db = database(cursor(value=2))
    dependency = deletions._dependency(db, kind, 1)
    assert dependency is not None
    assert dependency[1]["uuid"] == (EVIDENCE if kind == "relations" else OTHER)
    assert db.execute.call_count == 1
    monkeypatch.setattr(deletions, "_dependency", Mock(return_value=dependency))
    monkeypatch.setattr(
        deletions,
        "pending_blocker",
        Mock(
            return_value={
                "blocking_record": {"kind": "nodes", "id": NODE},
                "delete_job_id": NODE,
                "pending_since": "2026-01-01T00:00:00.000000Z",
            }
        ),
    )
    with pytest.raises(RecordConflictError, match="DEPENDENCIES_EXIST"):
        deletions._reject_dependency(db, kind, owner())


@pytest.mark.parametrize("kind", ["nodes", "relations", "evidence"])
def test_child_deletion_spends_remaining_budget(kind):
    db = database()
    db.changes.side_effect = [2, 3]
    assert deletions._delete_children(db, kind, 1, 5) == 5
    assert [call.args[1] for call in db.execute.call_args_list] == [(1, 5), (1, 3)]
    db = database(cursor(value=None), cursor(value=1))
    assert deletions._has_children(db, kind, 1)
    assert db.execute.call_count == 2


def test_incident_cleanup_prefers_source_then_target_and_counts_owner(monkeypatch):
    monkeypatch.setattr(deletions, "_delete_children", Mock(return_value=2))
    monkeypatch.setattr(deletions, "_has_children", Mock(return_value=False))
    db = database(cursor(), cursor(value=2), cursor())
    assert deletions._delete_incident(db, 1, 3) == 3
    assert db.execute.call_args.args[1] == (2,)


@pytest.mark.parametrize(
    ("kind", "children", "remaining", "expected"),
    [
        ("nodes", 2, False, (3, True, False)),
        ("relations", 3, False, (3, False, False)),
        ("relations", 1, True, (1, False, False)),
        ("evidence", 1, False, (1, False, True)),
    ],
)
def test_step_budget_and_files_boundary(monkeypatch, kind, children, remaining, expected):
    intent = deletions.DeleteIntent(kind, 1, NODE, OTHER, cascade=True, requested_at=10)
    monkeypatch.setattr(
        deletions,
        "row_by_id",
        Mock(return_value=owner(lifecycle="delete_pending", delete_job_id=OTHER, delete_requested_at=10)),
    )
    monkeypatch.setattr(deletions, "_delete_incident", Mock(return_value=0))
    monkeypatch.setattr(deletions, "_delete_children", Mock(return_value=children))
    monkeypatch.setattr(deletions, "_has_children", Mock(return_value=remaining))
    result = deletions.GraphDeletion.step(database(), intent, 3)
    assert (result.rows_deleted, result.done, result.files_pending) == expected


def test_step_rejects_mismatched_intent_and_invalid_budget(monkeypatch):
    intent = deletions.DeleteIntent("nodes", 1, NODE, OTHER, cascade=True, requested_at=10)
    monkeypatch.setattr(deletions, "row_by_id", Mock(return_value=owner()))
    for budget in (0, 101, True):
        with pytest.raises(InvalidParamsError):
            deletions.GraphDeletion.step(database(), intent, budget)
    with pytest.raises(ConflictError):
        deletions.GraphDeletion.step(database(), intent)
    monkeypatch.setattr(deletions, "row_by_id", Mock(return_value=None))
    assert deletions.GraphDeletion.step(database(), intent).done
