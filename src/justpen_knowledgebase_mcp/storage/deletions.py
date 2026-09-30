"""Atomic delete admission and logical-row-bounded SQL cleanup, without scheduling."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..catalog import scope_order
from ..errors import ConflictError, InvalidParamsError, MissingRecordsError, RecordConflictError
from ..responses import BlockerDetails, MissingDetails
from . import graph_sql as sql
from .graph import pending_blocker, require_ready, row_by_id

if TYPE_CHECKING:
    import apsw

    from ..models import DeleteRequest

# A rejection purges scoped nodes deepest type first, so each node goes before the parent that
# scopes it, then the rejected node's own relations. The catalog orders scoped types parent-first.
_REJECTION_NODE_TYPES = tuple(reversed(scope_order()))
_REJECTION_NODE_BY_TYPE = (
    "SELECT id,uuid,delete_job_id,delete_cascade,delete_requested_at FROM nodes "
    "WHERE type=? AND lifecycle='delete_pending' AND delete_job_id=? ORDER BY id LIMIT 1"
)
_REJECTION_INTENT = {
    "nodes": "SELECT id,uuid,delete_job_id,delete_cascade,delete_requested_at FROM nodes "
    "WHERE lifecycle='delete_pending' AND delete_job_id=? ORDER BY id LIMIT 1",
    "relations": "SELECT id,uuid,delete_job_id,delete_cascade,delete_requested_at FROM relations "
    "WHERE lifecycle='delete_pending' AND delete_job_id=? ORDER BY id LIMIT 1",
}
_INCIDENT = (
    "SELECT id,uuid,delete_job_id FROM relations WHERE source_id=? ORDER BY id LIMIT 1",
    "SELECT id,uuid,delete_job_id FROM relations WHERE target_id=? ORDER BY id LIMIT 1",
)
# A ready node linked to this evidence that holds a claim and keeps no other link to ready evidence
# outside the bound JSON array of deleted UUIDs (R21, KTD9). The claim values mirror the write path.
_BARE_CLAIM = (
    "SELECT n.uuid FROM node_evidence l JOIN nodes n ON n.id=l.node_id "
    "WHERE l.evidence_id=? AND n.lifecycle='ready' "
    "AND (n.ownership IN ('owned','dependency','rejected') OR n.authorization IN ('in_scope','out_of_scope') "
    "OR n.authorization_override IN ('in_scope','out_of_scope')) "
    "AND NOT EXISTS(SELECT 1 FROM node_evidence k JOIN evidence e ON e.id=k.evidence_id "
    "WHERE k.node_id=n.id AND e.lifecycle='ready' AND e.uuid NOT IN (SELECT value FROM json_each(?))) "
    "ORDER BY l.id LIMIT 1"
)


@dataclass(frozen=True)
class DeleteIntent:
    """Immutable authority persisted on a canonical owner, reusable after recovery."""

    kind: str
    owner_id: int
    uuid: str
    job_id: str
    cascade: bool
    requested_at: int


@dataclass(frozen=True)
class DeleteStep:
    """One transaction's exact logical row cost and remaining owner state."""

    rows_deleted: int
    done: bool
    files_pending: bool = False
    # UUIDs of this job's own intents removed in the step: the owner, and incident relations it held.
    purged: tuple[str, ...] = ()


@dataclass(frozen=True)
class RejectionStep:
    """One transaction's cost across a rejection job's intents, and whether any remain."""

    rows_deleted: int
    purged: tuple[str, ...]
    done: bool


def _dependency(connection: apsw.Connection, kind: str, owner_id: int) -> tuple[str, dict[str, Any]] | None:
    queries: list[tuple[str, str]]
    if kind == "nodes":
        queries = [
            ("relations", "SELECT id FROM relations WHERE source_id=? ORDER BY id LIMIT 1"),
            ("relations", "SELECT id FROM relations WHERE target_id=? ORDER BY id LIMIT 1"),
            ("evidence", "SELECT evidence_id FROM node_evidence WHERE node_id=? ORDER BY id LIMIT 1"),
        ]
    elif kind == "relations":
        queries = [("evidence", "SELECT evidence_id FROM relation_evidence WHERE relation_id=? ORDER BY id LIMIT 1")]
    else:
        queries = [
            ("nodes", "SELECT node_id FROM node_evidence WHERE evidence_id=? ORDER BY id LIMIT 1"),
            ("relations", "SELECT relation_id FROM relation_evidence WHERE evidence_id=? ORDER BY id LIMIT 1"),
        ]
    for dependency_kind, query in queries:
        identifier = connection.execute(query, (owner_id,)).get
        if identifier is not None:
            row = row_by_id(connection, dependency_kind, identifier)
            if row is None:
                raise ConflictError("owner disappeared")
            return dependency_kind, row
    return None


def _reject_dependency(connection: apsw.Connection, kind: str, row: dict[str, Any]) -> None:
    dependency = _dependency(connection, kind, row["id"])
    if dependency is None:
        return
    other_kind, other = dependency
    blocker = pending_blocker(connection, other_kind, other)
    details: dict[str, Any] = {"blocking_record": {"kind": other_kind, "id": other["uuid"]}}
    if blocker:
        details.update(delete_job_id=blocker["delete_job_id"], pending_since=blocker["pending_since"])
        if blocker["blocking_record"] != details["blocking_record"]:
            details["deletion_owner"] = blocker["blocking_record"]
    raise RecordConflictError("DEPENDENCIES_EXIST", BlockerDetails.model_validate(details))


def _reject_bare_claim(connection: apsw.Connection, request: DeleteRequest, row: dict[str, Any]) -> None:
    """Refuse deleting the last ready evidence of a node that holds a claim (R21, KTD9)."""
    node = connection.execute(_BARE_CLAIM, (row["id"], json.dumps(request.ids))).get
    if node is not None:
        raise RecordConflictError(
            "LAST_CLAIM_EVIDENCE", BlockerDetails.model_validate({"blocking_record": {"kind": "nodes", "id": node}})
        )


def _reject_scope_orphan(connection: apsw.Connection, kind: str, row: dict[str, Any]) -> None:
    """Keep every scoped child attached until deletion of the child removes its edge."""
    if kind == "relations":
        if row["type"] in sql.SCOPE_RELATION_TYPES and row_by_id(connection, "nodes", row["target_id"]) is not None:
            raise ConflictError("scoped child must be deleted first")
        return
    if (
        kind == "nodes"
        and connection.execute(sql.SCOPED_CHILD_BY_PARENT, (row["id"], sql.SCOPED_CHILD_RELATIONS)).get is not None
    ):
        raise ConflictError("scoped child must be deleted first")


def _delete_children(connection: apsw.Connection, kind: str, owner_id: int, budget: int) -> int:
    tables = (
        [("node_evidence", "node_id"), ("node_property_index", "owner_id"), ("search_documents", "node_id")]
        if kind == "nodes"
        else [
            ("relation_evidence", "relation_id"),
            ("relation_property_index", "owner_id"),
            ("search_documents", "relation_id"),
        ]
    )
    if kind == "evidence":
        tables = [
            ("node_evidence", "evidence_id"),
            ("relation_evidence", "evidence_id"),
            ("evidence_sources", "evidence_id"),
            ("search_documents", "evidence_id"),
        ]
    deleted = 0
    for table, column in tables:
        if deleted >= budget:
            break
        # Property tables have a compound PK; rowid remains available in v2.
        connection.execute(
            sql.CHILD_DELETE[table, column],
            (owner_id, budget - deleted),
        )
        deleted += connection.changes()
    return deleted


def _has_children(connection: apsw.Connection, kind: str, owner_id: int) -> bool:
    if kind == "evidence":
        tables = [
            ("node_evidence", "evidence_id"),
            ("relation_evidence", "evidence_id"),
            ("evidence_sources", "evidence_id"),
            ("search_documents", "evidence_id"),
        ]
    else:
        singular = "node" if kind == "nodes" else "relation"
        tables = [
            (f"{singular}_evidence", f"{singular}_id"),
            (f"{singular}_property_index", "owner_id"),
            ("search_documents", f"{singular}_id"),
        ]
    return any(
        connection.execute(sql.CHILD_EXISTS[table, column], (owner_id,)).get is not None for table, column in tables
    )


def _delete_incident(
    connection: apsw.Connection, owner_id: int, row_budget: int, job_id: str
) -> tuple[int, tuple[str, ...]]:
    """Delete incident relations; also return the removed ones that were this job's own intents."""
    deleted = 0
    purged: list[str] = []
    while deleted < row_budget:
        relation = connection.execute(_INCIDENT[0], (owner_id,)).get
        if relation is None:
            relation = connection.execute(_INCIDENT[1], (owner_id,)).get
        if relation is None:
            break
        identifier, uuid, delete_job_id = relation
        deleted += _delete_children(connection, "relations", identifier, row_budget - deleted)
        if deleted < row_budget and not _has_children(connection, "relations", identifier):
            connection.execute("DELETE FROM relations WHERE id=?", (identifier,))
            deleted += connection.changes()
            if delete_job_id == job_id:
                purged.append(uuid)
    return deleted, tuple(purged)


def _next_rejection_intent(connection: apsw.Connection, job_id: str) -> DeleteIntent | None:
    """Select the deepest pending scoped node, then any other node, then a relation of this job."""
    queries = [(_REJECTION_NODE_BY_TYPE, (type_name, job_id), "nodes") for type_name in _REJECTION_NODE_TYPES]
    queries += [(_REJECTION_INTENT[kind], (job_id,), kind) for kind in ("nodes", "relations")]
    for query, bindings, kind in queries:
        row = connection.execute(query, bindings).fetchone()
        if row is not None:
            return DeleteIntent(kind, row[0], row[1], row[2], bool(row[3]), row[4])
    return None


def _validated_delete_rows(connection: apsw.Connection, request: DeleteRequest) -> list[dict[str, Any]]:
    """Resolve and validate every deletion target before any intent is stored."""
    rows = [row_by_id(connection, request.kind, identifier) for identifier in request.ids]
    missing = [identifier for identifier, row in zip(request.ids, rows, strict=True) if row is None]
    if missing:
        raise MissingRecordsError(MissingDetails.model_validate({"missing_ids": missing}))
    validated: list[dict[str, Any]] = []
    for row in rows:
        if row is None:
            raise ConflictError("owner disappeared")
        require_ready(connection, request.kind, row)
        _reject_scope_orphan(connection, request.kind, row)
        if not request.cascade:
            _reject_dependency(connection, request.kind, row)
        if request.kind == "evidence":
            _reject_bare_claim(connection, request, row)
        validated.append(row)
    return validated


class GraphDeletion:
    """Primitives require the caller's existing guarded BEGIN IMMEDIATE snapshot."""

    @staticmethod
    def prepare(connection: apsw.Connection, request: DeleteRequest, job_id: str) -> list[DeleteIntent]:
        """Validate the entire batch before persisting immutable owner intents."""
        if len(set(request.ids)) != len(request.ids):
            raise InvalidParamsError("duplicate delete ids")
        rows = _validated_delete_rows(connection, request)
        requested_at = time.time_ns() // 1000
        intents: list[DeleteIntent] = []
        for row in rows:
            connection.execute(
                sql.OWNER_PENDING[request.kind],
                (job_id, int(request.cascade), requested_at, row["id"]),
            )
            intents.append(DeleteIntent(request.kind, row["id"], row["uuid"], job_id, request.cascade, requested_at))
        return intents

    @staticmethod
    def step(connection: apsw.Connection, intent: DeleteIntent, row_budget: int = 100) -> DeleteStep:
        """Delete at most 100 canonical/link/derived logical rows, never files."""
        if type(row_budget) is not int or not 1 <= row_budget <= 100:
            raise InvalidParamsError("invalid deletion row budget")
        owner = row_by_id(connection, intent.kind, intent.owner_id)
        if owner is None:
            return DeleteStep(0, done=True)
        if (
            owner["uuid"],
            owner["delete_job_id"],
            owner["delete_cascade"],
            owner["delete_requested_at"],
            owner["lifecycle"],
        ) != (intent.uuid, intent.job_id, int(intent.cascade), intent.requested_at, "delete_pending"):
            raise ConflictError("delete intent mismatch")
        deleted, purged = 0, ()
        if intent.kind == "nodes":
            deleted, purged = _delete_incident(connection, intent.owner_id, row_budget, intent.job_id)
            if deleted == row_budget:
                return DeleteStep(deleted, done=False, purged=purged)
        deleted += _delete_children(connection, intent.kind, intent.owner_id, row_budget - deleted)
        if deleted == row_budget or _has_children(connection, intent.kind, intent.owner_id):
            return DeleteStep(deleted, done=False, purged=purged)
        if intent.kind == "evidence":
            return DeleteStep(deleted, done=False, files_pending=True)
        connection.execute(sql.OWNER_DELETE[intent.kind], (intent.owner_id,))
        return DeleteStep(deleted + connection.changes(), done=True, purged=(*purged, intent.uuid))

    @staticmethod
    def purge_rejection(connection: apsw.Connection, job_id: str, row_budget: int = 100) -> RejectionStep:
        """Spend at most 100 logical rows across a rejection job's intents, innermost first (KTD5).

        The job owns the rejected node's pending scoped descendants and incident relations, never
        the node itself, so no `kb_delete` admission check applies to them.
        """
        if type(row_budget) is not int or not 1 <= row_budget <= 100:
            raise InvalidParamsError("invalid deletion row budget")
        deleted = 0
        purged: list[str] = []
        while deleted < row_budget:
            intent = _next_rejection_intent(connection, job_id)
            if intent is None:
                return RejectionStep(deleted, tuple(purged), done=True)
            step = GraphDeletion.step(connection, intent, row_budget - deleted)
            deleted += step.rows_deleted
            purged.extend(step.purged)
            if not step.done:
                break
        return RejectionStep(deleted, tuple(purged), done=False)
