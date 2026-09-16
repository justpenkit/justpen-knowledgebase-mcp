"""Mutable graph primitives inside an existing admitted worker transaction."""

from __future__ import annotations

import ipaddress
import json
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import uuid4

from ..catalog import catalog_manifest, catalog_schema, validate_record
from ..cursors import CursorBinding
from ..errors import ConflictError, InvalidParamsError, NotFoundError, RecordConflictError
from ..identity import format_timestamp, identity_json, identity_key, parse_timestamp
from ..models import GetRequest, Mutation, NodeRef, RelationWrite, WriteRequest, WriteResult
from ..mutations import canonical_json, merge_properties
from ..responses import BlockerDetails
from . import fulltext, graph_sql as sql
from .properties import refresh_properties

if TYPE_CHECKING:
    import apsw

    from ..models import TypesRequest
    from .worker import OperationToken


KINDS = frozenset(("nodes", "relations", "evidence"))


def row_by_id(connection: apsw.Connection, kind: str, identifier: str | int) -> dict[str, Any] | None:
    """Materialize one canonical owner by UUID or internal primary key."""
    if kind not in KINDS:
        raise InvalidParamsError("unknown record kind")
    column = "id" if type(identifier) is int else "uuid"
    cursor = connection.execute(sql.OWNER_LOOKUP[kind, column], (identifier,))
    row = cursor.fetchone()
    if row is None:
        return None
    names = [item[0] for item in cursor.get_description()]
    return dict(zip(names, row, strict=True))


def pending_blocker(connection: apsw.Connection, kind: str, row: dict[str, Any]) -> dict[str, Any] | None:
    """Select own intent, source, then target; never propagate edge state to nodes."""
    if row["lifecycle"] == "delete_pending":
        return {
            "blocking_record": {"kind": kind, "id": row["uuid"]},
            "delete_job_id": row["delete_job_id"],
            "pending_since": format_timestamp(row["delete_requested_at"]),
        }
    if kind == "relations":
        for field in ("source_id", "target_id"):
            endpoint = row_by_id(connection, "nodes", row[field])
            if endpoint is None:
                raise NotFoundError("relation endpoint missing")
            blocker = pending_blocker(connection, "nodes", endpoint)
            if blocker is not None:
                return blocker
    return None


def require_ready(connection: apsw.Connection, kind: str, row: dict[str, Any]) -> None:
    """Reject pending owners and effectively pending relations with typed details."""
    blocker = pending_blocker(connection, kind, row)
    if blocker is not None:
        raise RecordConflictError("RECORD_DELETING", BlockerDetails.model_validate(blocker))


def _ref(connection: apsw.Connection, ref: NodeRef, nodes: list[dict[str, Any]]) -> dict[str, Any]:
    if ref.node_index is not None:
        if ref.node_index >= len(nodes):
            raise InvalidParamsError("node_index outside batch")
        return nodes[ref.node_index]
    row = row_by_id(connection, "nodes", ref.id or "")
    if row is None:
        raise NotFoundError("node reference missing")
    return row


def _validate_endpoints(type_name: str, source: dict[str, Any], target: dict[str, Any]) -> None:
    definition = catalog_manifest()["relations"].get(type_name)
    if definition is None or source["type"] not in definition["sources"] or target["type"] not in definition["targets"]:
        raise InvalidParamsError("relation endpoint types are not allowed")
    if source["id"] == target["id"] and not definition["self_edge"]:
        raise InvalidParamsError("self edge is not allowed")
    first, second = json.loads(source["properties"]), json.loads(target["properties"])
    valid = True
    if type_name == "name_in_domain":
        valid = first["name"] == second["name"] or first["name"].endswith("." + second["name"])
    elif type_name == "subdomain_of":
        valid = first["name"].endswith("." + second["name"])
    elif type_name == "offers_service":
        valid = first.get("address", first.get("name")) == second["host"]
    elif type_name == "member_of":
        valid = second["kind"] == "group" and first["realm"] == second["realm"]
    elif type_name == "serves_endpoint":
        url = urlsplit(str(second["url"]))
        try:
            ipaddress.ip_address(first["host"])
            host_matches = True
        except ValueError:
            host_matches = first["host"] == url.hostname
        valid = (
            first["transport"] == "tcp"
            and host_matches
            and first["port"] == (url.port or (443 if url.scheme == "https" else 80))
        )
    if not valid:
        raise InvalidParamsError("relation endpoint constraint failed")


def _endpoints(
    connection: apsw.Connection,
    mutation: Mutation,
    nodes: list[dict[str, Any]],
    row: dict[str, Any] | None,
    type_name: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    source = target = None
    if isinstance(mutation, RelationWrite):
        source = (
            _ref(connection, mutation.source_ref, nodes)
            if mutation.source_ref
            else row_by_id(connection, "nodes", row["source_id"])
            if row
            else None
        )
        target = (
            _ref(connection, mutation.target_ref, nodes)
            if mutation.target_ref
            else row_by_id(connection, "nodes", row["target_id"])
            if row
            else None
        )
        if source is None or target is None:
            raise NotFoundError("relation endpoint missing")
        if row and (source["id"] != row["source_id"] or target["id"] != row["target_id"]):
            raise ConflictError("endpoints are immutable")
        _validate_endpoints(type_name, source, target)
    return source, target


def _deduplicate(
    connection: apsw.Connection,
    kind: str,
    mutation: Mutation,
    row: dict[str, Any] | None,
    type_name: str,
    endpoints: tuple[dict[str, Any] | None, dict[str, Any] | None],
) -> dict[str, Any] | None:
    source, target = endpoints
    if row is None:
        key = identity_key(kind, type_name, mutation.properties)
        if kind == "nodes":
            identifier = connection.execute("SELECT id FROM nodes WHERE type=? AND key=?", (type_name, key)).get
        else:
            if source is None or target is None:
                raise InvalidParamsError("endpoints required")
            identifier = connection.execute(
                "SELECT id FROM relations WHERE source_id=? AND type=? AND target_id=? AND key=?",
                (source["id"], type_name, target["id"], key),
            ).get
        if identifier is not None:
            row = row_by_id(connection, kind, identifier)
            if row is None:
                raise NotFoundError("record disappeared")
            require_ready(connection, kind, row)
            if identity_json(kind, type_name, json.loads(row["properties"])) != identity_json(
                kind, type_name, mutation.properties
            ):
                raise ConflictError("identity hash collision")
    return row


def _persist(
    connection: apsw.Connection,
    kind: str,
    mutation: Mutation,
    row: dict[str, Any] | None,
    properties: dict[str, Any],
    endpoints: tuple[dict[str, Any] | None, dict[str, Any] | None],
) -> tuple[dict[str, Any], bool]:
    source, target = endpoints
    type_name = row["type"] if row else mutation.type
    if type_name is None:
        raise InvalidParamsError("type required")
    key = identity_key(kind, type_name, properties)
    now = time.time_ns() // 1000
    metadata: dict[str, Any] = json.loads(row["metadata"]) if row else {"source": None}
    if kind == "nodes":
        metadata.setdefault("label", None)
    for field in ("source", "label"):
        if field in mutation.model_fields_set:
            metadata[field] = getattr(mutation, field)
    observed_at = parse_timestamp(mutation.observed_at) if mutation.observed_at is not None else now
    created = row is None
    if row is None:
        identifier = str(uuid4())
        if kind == "nodes":
            connection.execute(
                "INSERT INTO nodes(uuid,type,key,properties,metadata,created_at,updated_at,observed_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    type_name,
                    key,
                    canonical_json(properties),
                    canonical_json(metadata),
                    now,
                    now,
                    observed_at,
                ),
            )
        else:
            if source is None or target is None:
                raise InvalidParamsError("endpoints required")
            connection.execute(
                "INSERT INTO relations(uuid,source_id,type,target_id,key,properties,metadata,created_at,updated_at,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    source["id"],
                    type_name,
                    target["id"],
                    key,
                    canonical_json(properties),
                    canonical_json(metadata),
                    now,
                    now,
                    observed_at,
                ),
            )
        row = row_by_id(connection, kind, identifier)
        if row is None:
            raise NotFoundError("record disappeared")
    else:
        connection.execute(
            sql.OWNER_UPDATE[kind],
            (canonical_json(properties), canonical_json(metadata), now, observed_at, row["id"]),
        )
        row = row_by_id(connection, kind, row["id"])
        if row is None:
            raise NotFoundError("record disappeared")
    refresh_properties(connection, kind, row, properties)
    fulltext.refresh_record_text(connection, kind, row)
    return row, created


def _upsert(
    connection: apsw.Connection, kind: str, mutation: Mutation, nodes: list[dict[str, Any]], seen: set[tuple[str, int]]
) -> tuple[dict[str, Any], bool]:
    row = row_by_id(connection, kind, mutation.id) if mutation.id is not None else None
    if mutation.id is not None and row is None:
        raise NotFoundError("patch target missing")
    if row is not None:
        require_ready(connection, kind, row)
    type_name = row["type"] if row is not None else mutation.type
    if type_name is None:
        raise InvalidParamsError("type required")
    if mutation.type is not None and mutation.type != type_name:
        raise ConflictError("type is immutable")
    source, target = _endpoints(connection, mutation, nodes, row, type_name)
    row = _deduplicate(connection, kind, mutation, row, type_name, (source, target))
    for endpoint in (source, target):
        if endpoint is not None:
            require_ready(connection, "nodes", endpoint)
    current: dict[str, Any] = json.loads(row["properties"]) if row else {}
    properties = merge_properties(current, mutation.properties, mutation.remove_properties)
    validate_record(kind, type_name, properties)
    if row and identity_json(kind, type_name, current) != identity_json(kind, type_name, properties):
        raise ConflictError("identity properties are immutable")
    if row is not None and (kind, row["id"]) in seen:
        raise InvalidParamsError("duplicate batch identity")
    row, created = _persist(connection, kind, mutation, row, properties, (source, target))
    identity = (kind, row["id"])
    seen.add(identity)
    return row, created


def _links(connection: apsw.Connection, kind: str, row: dict[str, Any], mutation: Mutation) -> tuple[int, int]:
    added = removed = 0
    for operation, identifiers in (("add", mutation.evidence_add), ("remove", mutation.evidence_remove)):
        for identifier in identifiers:
            evidence = row_by_id(connection, "evidence", identifier)
            if evidence is None:
                raise NotFoundError("evidence reference missing")
            require_ready(connection, "evidence", evidence)
            if operation == "add":
                connection.execute(sql.LINK_ADD[kind], (row["id"], evidence["id"]))
                added += connection.changes()
            else:
                connection.execute(sql.LINK_REMOVE[kind], (row["id"], evidence["id"]))
                removed += connection.changes()
    return added, removed


class Graph:
    """Materialize all graph outputs before the worker commits or ends its read."""

    @staticmethod
    def write(connection: apsw.Connection, token: OperationToken, request: WriteRequest) -> dict[str, Any]:
        """Validate and merge against latest rows under the existing write lock."""
        output: dict[str, Any] = {"nodes": [], "relations": []}
        nodes: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        try:
            for kind, mutations in (("nodes", request.nodes), ("relations", request.relations)):
                for mutation in mutations:
                    token.check()
                    row, created = _upsert(connection, kind, mutation, nodes, seen)
                    added, removed = _links(connection, kind, row, mutation)
                    output[kind].append(
                        {
                            "id": row["uuid"],
                            "created": created,
                            "updated": not created,
                            "links_added": added,
                            "links_removed": removed,
                            "property_index": json.loads(row["metadata"])["property_index"],
                        }
                    )
                    if kind == "nodes":
                        nodes.append(row)
        except ValueError as exc:
            raise InvalidParamsError("invalid graph mutation") from exc
        return WriteResult.model_validate(output).model_dump()

    @staticmethod
    def get(connection: apsw.Connection, token: OperationToken, request: GetRequest) -> dict[str, Any]:
        """Return whole records with explicit missing and response-budget remainder."""
        if request.view != "record":
            return _associations(connection, request)
        output: dict[str, Any] = {"records": [], "missing_ids": [], "remaining_ids": []}
        for identifier in request.ids:
            token.check()
            row = row_by_id(connection, request.kind, identifier)
            if row is None:
                output["missing_ids"].append(identifier)
                continue
            record = _record(connection, request.kind, row)
            output["records"].append(record)
            if len(canonical_json(output).encode("utf-8")) > 250000:
                output["records"].pop()
                output["remaining_ids"].append(identifier)
        return output


def _record(connection: apsw.Connection, kind: str, row: dict[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": row["uuid"],
        "created_at": format_timestamp(row["created_at"]) if row["created_at"] is not None else None,
        "updated_at": format_timestamp(row["updated_at"]) if row["updated_at"] is not None else None,
    }
    if kind != "evidence":
        metadata: dict[str, Any] = json.loads(row["metadata"])
        record.update(metadata)
        record.update(type=row["type"], key=row["key"], properties=json.loads(row["properties"]))
        record["observed_at"] = format_timestamp(row["observed_at"]) if row["observed_at"] is not None else None
        record["link_count"] = connection.execute(sql.LINK_COUNT[kind], (row["id"],)).get
        if kind == "relations":
            for field in ("source_id", "target_id"):
                record[field] = connection.execute("SELECT uuid FROM nodes WHERE id=?", (row[field],)).get
    else:
        record.update({field: row[field] for field in ("sha256", "byte_size", "media_type", "encoding", "index_state")})
        record["link_count"] = sum(
            connection.execute(sql.EVIDENCE_LINK_COUNT[table], (row["id"],)).get
            for table in ("node_evidence", "relation_evidence")
        )
        record["incomplete"] = bool(row["incomplete"])
        record["source_count"] = connection.execute(
            "SELECT count(*) FROM evidence_sources WHERE evidence_id=?", (row["id"],)
        ).get
    blocker = pending_blocker(connection, kind, row)
    record["lifecycle"] = "delete_pending" if blocker else "ready"
    record["delete_job_id"] = blocker["delete_job_id"] if blocker else None
    record["pending_since"] = blocker["pending_since"] if blocker else None
    return record


def _binding(
    connection: apsw.Connection, kind: str, owner: str | None, view: str, query: dict[str, Any]
) -> CursorBinding:

    workspace_id, epoch = connection.execute("SELECT workspace_id,query_epoch FROM settings WHERE singleton=1").get
    return CursorBinding(workspace_id, epoch, kind, owner, view, query)


def _associations(connection: apsw.Connection, request: GetRequest) -> dict[str, Any]:
    owner = row_by_id(connection, request.kind, request.ids[0])
    if owner is None:
        raise NotFoundError("association owner missing")
    require_ready(connection, request.kind, owner)
    binding = _binding(
        connection,
        request.kind,
        request.ids[0],
        request.view,
        {"kind": request.kind, "id": request.ids[0], "view": request.view},
    )
    try:
        after, after_kind = binding.decode_position(request.cursor) if request.cursor else (0, None)
    except ValueError as exc:
        raise InvalidParamsError("invalid cursor") from exc
    if request.view == "sources":
        rows = [
            (row[0], {"source": row[1], "first_seen_at": row[2], "last_seen_at": row[3]})
            for row in connection.execute(
                "SELECT id,source,first_seen_at,last_seen_at FROM evidence_sources WHERE evidence_id=? AND id>? ORDER BY id LIMIT ?",
                (owner["id"], after, request.limit + 1),
            )
        ]
    elif request.kind != "evidence":
        rows = list(
            connection.execute(
                sql.LINK_PAGE[request.kind],
                (owner["id"], after, request.limit + 1),
            )
        )
    else:
        rows: list[tuple[int, Any]] = []
        for kind in ("nodes", "relations"):
            lower_bound = after - 1 if after_kind == "nodes" and kind == "relations" else after
            rows.extend(
                (row[0], {"kind": kind, "id": row[1]})
                for row in connection.execute(
                    sql.TARGET_PAGE[kind],
                    (owner["id"], lower_bound, request.limit + 1),
                )
            )
        rows.sort(key=lambda row: (row[0], row[1]["kind"]))
    items: list[Any] = []
    last = after
    last_kind = after_kind
    truncated = False
    for index, row in enumerate(rows):
        if index == request.limit or len(canonical_json([*items, row[1]]).encode("utf-8")) > 245000:
            truncated = True
            break
        last = row[0]
        if request.kind == "evidence" and request.view == "links":
            last_kind = row[1]["kind"]
        items.append(row[1])
    return {request.view: items, "next_cursor": binding.encode(last, last_kind) if truncated else None}


def graph_types(connection: apsw.Connection, token: OperationToken, request: TypesRequest) -> dict[str, Any]:
    """Discover unused types and ready-only counts within the read deadline."""
    manifest = catalog_manifest()
    definitions = manifest[request.kind]
    if request.type is not None and request.type not in definitions:
        raise InvalidParamsError("unknown catalog type")
    binding = _binding(connection, request.kind, None, "types", {"type": request.type, "kind": request.kind})
    try:
        after = binding.decode(request.cursor) if request.cursor else 0
    except ValueError as exc:
        raise InvalidParamsError("invalid cursor") from exc
    names = [request.type] if request.type is not None else sorted(definitions)
    items: list[Any] = []
    deferred = False
    for name in names[after : after + request.limit]:
        token.check()
        count = None
        if token.deadline - time.monotonic() > 0.1:
            if request.kind == "nodes":
                count = connection.execute("SELECT count(*) FROM nodes WHERE type=? AND lifecycle='ready'", (name,)).get
            else:
                count = connection.execute(
                    "SELECT count(*) FROM relations r JOIN nodes s ON s.id=r.source_id JOIN nodes t ON t.id=r.target_id WHERE r.type=? AND r.lifecycle='ready' AND s.lifecycle='ready' AND t.lifecycle='ready'",
                    (name,),
                ).get
        else:
            deferred = True
        items.append(
            {"type": name, **definitions[name], "properties_schema": catalog_schema(request.kind, name), "count": count}
        )
    return {
        "types": items,
        "counts_deferred": deferred,
        "common": manifest["common"],
        "formats": manifest["formats"],
        "next_cursor": binding.encode(after + len(items)) if after + len(items) < len(names) else None,
    }
