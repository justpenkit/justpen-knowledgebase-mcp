"""Atomic delete admission and logical-row-bounded SQL cleanup, without scheduling."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..errors import ConflictError, InvalidParamsError, MissingRecordsError, RecordConflictError
from ..responses import BlockerDetails, MissingDetails
from . import graph_sql as sql
from .graph import pending_blocker, require_ready, row_by_id

if TYPE_CHECKING:
    import apsw

    from ..models import DeleteRequest


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


def _reject_scope_orphan(connection: apsw.Connection, kind: str, row: dict[str, Any]) -> None:
    """Keep every scoped child attached until deletion of the child removes its edge."""
    if kind == "relations":
        if row["type"] in sql.SCOPE_RELATION_TYPES and row_by_id(connection, "nodes", row["target_id"]) is not None:
            raise ConflictError("scoped child must be deleted first")
        return
    if kind == "nodes" and connection.execute(sql.SCOPED_CHILD_BY_PARENT, (row["id"],)).get is not None:
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


def _delete_incident(connection: apsw.Connection, owner_id: int, row_budget: int) -> int:
    deleted = 0
    while deleted < row_budget:
        identifier = connection.execute(
            "SELECT id FROM relations WHERE source_id=? ORDER BY id LIMIT 1", (owner_id,)
        ).get
        if identifier is None:
            identifier = connection.execute(
                "SELECT id FROM relations WHERE target_id=? ORDER BY id LIMIT 1", (owner_id,)
            ).get
        if identifier is None:
            break
        deleted += _delete_children(connection, "relations", identifier, row_budget - deleted)
        if deleted < row_budget and not _has_children(connection, "relations", identifier):
            connection.execute("DELETE FROM relations WHERE id=?", (identifier,))
            deleted += connection.changes()
    return deleted


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
        deleted = 0
        if intent.kind == "nodes":
            deleted = _delete_incident(connection, intent.owner_id, row_budget)
            if deleted == row_budget:
                return DeleteStep(deleted, done=False)
        deleted += _delete_children(connection, intent.kind, intent.owner_id, row_budget - deleted)
        if deleted == row_budget or _has_children(connection, intent.kind, intent.owner_id):
            return DeleteStep(deleted, done=False)
        if intent.kind == "evidence":
            return DeleteStep(deleted, done=False, files_pending=True)
        connection.execute(sql.OWNER_DELETE[intent.kind], (intent.owner_id,))
        return DeleteStep(deleted + connection.changes(), done=True)
