"""Mutable graph primitives inside an existing admitted worker transaction."""

from __future__ import annotations

import ipaddress
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from ..catalog import catalog_manifest, catalog_schema, validate_record
from ..cursors import CursorBinding
from ..errors import ConflictError, ExpectedValidationError, InvalidParamsError, NotFoundError, RecordConflictError
from ..identity import format_timestamp, identity_json, identity_key, parse_timestamp
from ..models import GetRequest, Mutation, NodeRef, NodeWrite, RelationWrite, WriteRequest, WriteResult
from ..mutations import canonical_json, merge_properties
from ..responses import BlockerDetails
from . import fulltext, graph_sql as sql
from .properties import refresh_properties

if TYPE_CHECKING:
    import apsw

    from ..models import TypesRequest
    from .worker import OperationToken


KINDS = frozenset(("nodes", "relations", "evidence"))
_SCOPED_NODE_ORDER = ("port", "service", "finding")


@dataclass
class _PreparedMutation:
    """One fully validated mutation whose public identity is fixed before SQL writes."""

    mutation: Mutation
    type_name: str
    existing: dict[str, Any] | None
    row: dict[str, Any]
    properties: dict[str, Any]
    parent_id: str | None = None
    endpoints: tuple[dict[str, Any] | None, dict[str, Any] | None] = (None, None)


def _proper_subnet(
    source: ipaddress.IPv4Network | ipaddress.IPv6Network,
    target: ipaddress.IPv4Network | ipaddress.IPv6Network,
) -> bool:
    if isinstance(source, ipaddress.IPv4Network):
        return isinstance(target, ipaddress.IPv4Network) and target != source and target.subnet_of(source)
    return isinstance(target, ipaddress.IPv6Network) and target != source and target.subnet_of(source)


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


def _identity_definition(kind: str, type_name: str) -> dict[str, Any]:
    """Return one copied identity declaration from the fixed catalog."""
    return cast("dict[str, Any]", catalog_manifest()[kind][type_name]["identity"])


def _scope(type_name: str) -> dict[str, str] | None:
    """Return the parent-scope declaration for a node type, when present."""
    value = _identity_definition("nodes", type_name).get("scope")
    return cast("dict[str, str]", value) if isinstance(value, dict) else None


def _scope_parent(connection: apsw.Connection, child: dict[str, Any], relation_type: str) -> dict[str, Any]:
    """Resolve the sole persisted parent relation for an existing scoped node."""
    relations = list(
        connection.execute(
            "SELECT id,source_id FROM relations WHERE type=? AND target_id=? ORDER BY id LIMIT 2",
            (relation_type, child["id"]),
        )
    )
    if len(relations) != 1:
        raise ConflictError("scoped node must have exactly one parent relation")
    relation = row_by_id(connection, "relations", relations[0][0])
    parent = row_by_id(connection, "nodes", relations[0][1])
    if relation is None or parent is None:
        raise ConflictError("scoped parent relation is incomplete")
    require_ready(connection, "relations", relation)
    require_ready(connection, "nodes", parent)
    return parent


def _prepared_ref(
    connection: apsw.Connection, ref: NodeRef | None, nodes: list[dict[str, Any] | None]
) -> dict[str, Any]:
    """Resolve a reference against preflight rows without depending on request order."""
    if ref is None:
        raise InvalidParamsError("relation endpoint missing")
    if ref.node_index is not None:
        if ref.node_index >= len(nodes):
            raise InvalidParamsError("node_index outside batch")
        row = nodes[ref.node_index]
        if row is None:
            raise InvalidParamsError("scope parent could not be resolved")
        return row
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
    if type_name == "has_subdomain":
        valid = second["value"].endswith("." + first["value"])
    elif type_name == "contains_ip":
        source_network = ipaddress.ip_network(first["value"], strict=True)
        target_address = ipaddress.ip_address(second["value"])
        valid = first["version"] == second["version"] and source_network.version == target_address.version
        valid = valid and target_address in source_network
    elif type_name == "contains_cidr":
        source_network = ipaddress.ip_network(first["value"], strict=True)
        target_network = ipaddress.ip_network(second["value"], strict=True)
        valid = first["version"] == second["version"] and _proper_subnet(source_network, target_network)
    if not valid:
        raise InvalidParamsError("relation endpoint constraint failed")


def _node_header(connection: apsw.Connection, mutation: NodeWrite) -> tuple[dict[str, Any] | None, str]:
    """Resolve immutable node fields before dependency-ordered preflight."""
    existing = row_by_id(connection, "nodes", mutation.id) if mutation.id is not None else None
    if mutation.id is not None and existing is None:
        raise NotFoundError("patch target missing")
    if existing is not None:
        require_ready(connection, "nodes", existing)
    type_name = cast("str | None", existing["type"] if existing is not None else mutation.type)
    if type_name is None:
        raise InvalidParamsError("type required")
    if mutation.type is not None and mutation.type != type_name:
        raise ConflictError("type is immutable")
    definition = catalog_manifest()["nodes"].get(type_name)
    if definition is None:
        raise ExpectedValidationError("unknown catalog type")
    return existing, type_name


def _node_parent_id(
    connection: apsw.Connection,
    type_name: str,
    existing: dict[str, Any] | None,
    parent: dict[str, Any] | None,
) -> str | None:
    """Resolve the immutable public parent UUID required by a scoped identity."""
    scope = _scope(type_name)
    if scope is None:
        return None
    if existing is None:
        if parent is None:
            raise InvalidParamsError(f"new {type_name} requires exactly one {scope['relation']} relation")
        return cast("str", parent["uuid"])
    stored_parent = _scope_parent(connection, existing, scope["relation"])
    if parent is not None and parent["uuid"] != stored_parent["uuid"]:
        raise InvalidParamsError("scoped node cannot be attached to a different parent")
    return cast("str", stored_parent["uuid"])


def _deduplicate_node(
    connection: apsw.Connection,
    mutation: NodeWrite,
    existing: dict[str, Any] | None,
    type_name: str,
    properties: dict[str, Any],
    parent_id: str | None,
    key: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Match one node identity while distinguishing an impossible hash collision."""
    if existing is not None:
        current = json.loads(existing["properties"])
        if identity_json("nodes", type_name, current, parent_id) != identity_json(
            "nodes", type_name, properties, parent_id
        ):
            raise ConflictError("identity properties are immutable")
        return existing, properties
    identifier = connection.execute("SELECT id FROM nodes WHERE type=? AND key=?", (type_name, key)).get
    if identifier is None:
        return None, properties
    existing = row_by_id(connection, "nodes", identifier)
    if existing is None:
        raise NotFoundError("record disappeared")
    require_ready(connection, "nodes", existing)
    scope = _scope(type_name)
    if scope is not None and _scope_parent(connection, existing, scope["relation"])["uuid"] != parent_id:
        raise ConflictError("identity hash collision")
    stored = json.loads(existing["properties"])
    if identity_json("nodes", type_name, stored, parent_id) != identity_json("nodes", type_name, properties, parent_id):
        raise ConflictError("identity hash collision")
    properties = merge_properties(stored, mutation.properties, mutation.remove_properties)
    validate_record("nodes", type_name, properties)
    return existing, properties


def _preflight_row(
    existing: dict[str, Any] | None,
    type_name: str,
    key: str,
    properties: dict[str, Any],
    synthetic_id: int,
) -> dict[str, Any]:
    """Build an endpoint-compatible row with a stable public UUID before insertion."""
    if existing is not None:
        row = dict(existing)
        row["properties"] = canonical_json(properties)
        return row
    return {
        "id": synthetic_id,
        "uuid": str(uuid4()),
        "type": type_name,
        "key": key,
        "properties": canonical_json(properties),
        "metadata": "{}",
        "lifecycle": "ready",
    }


def _prepare_node(
    connection: apsw.Connection,
    mutation: NodeWrite,
    existing: dict[str, Any] | None,
    type_name: str,
    parent: dict[str, Any] | None,
    synthetic_id: int,
) -> _PreparedMutation:
    """Validate, scope, and deduplicate one node without changing SQLite state."""
    parent_id = _node_parent_id(connection, type_name, existing, parent)
    current: dict[str, Any] = json.loads(existing["properties"]) if existing is not None else {}
    properties = merge_properties(current, mutation.properties, mutation.remove_properties)
    validate_record("nodes", type_name, properties)
    key = identity_key("nodes", type_name, properties, parent_id)
    existing, properties = _deduplicate_node(connection, mutation, existing, type_name, properties, parent_id, key)
    row = _preflight_row(existing, type_name, key, properties, synthetic_id)
    return _PreparedMutation(mutation, type_name, existing, row, properties, parent_id)


def _scope_relation_for_node(node_index: int, type_name: str, relations: list[RelationWrite]) -> RelationWrite:
    """Select exactly one same-request relation that supplies a new node's scope."""
    scope = _scope(type_name)
    if scope is None:
        raise InvalidParamsError("node type is not scoped")
    matches = [
        relation
        for relation in relations
        if relation.id is None
        and relation.type == scope["relation"]
        and relation.target_ref is not None
        and relation.target_ref.node_index == node_index
    ]
    if len(matches) != 1:
        raise InvalidParamsError(f"new {type_name} requires exactly one {scope['relation']} relation")
    return matches[0]


def _enforce_single_parent(
    connection: apsw.Connection, relation_type: str, source: dict[str, Any], target: dict[str, Any]
) -> None:
    """Reject a persisted scoped child relation from any second source."""
    scope = _scope(cast("str", target["type"]))
    if scope is None or scope["relation"] != relation_type or target["id"] < 0:
        return
    sources = list(
        connection.execute(
            "SELECT source_id FROM relations WHERE type=? AND target_id=? ORDER BY id LIMIT 2",
            (relation_type, target["id"]),
        )
    )
    if any(source_id != source["id"] for (source_id,) in sources):
        raise InvalidParamsError("scoped node cannot be attached to a different parent")
    if len(sources) > 1:
        raise ConflictError("scoped node has multiple parent relations")


def _relation_header(connection: apsw.Connection, mutation: RelationWrite) -> tuple[dict[str, Any] | None, str]:
    """Resolve immutable relation fields before endpoint validation."""
    existing = row_by_id(connection, "relations", mutation.id) if mutation.id is not None else None
    if mutation.id is not None and existing is None:
        raise NotFoundError("patch target missing")
    if existing is not None:
        require_ready(connection, "relations", existing)
    type_name = cast("str | None", existing["type"] if existing is not None else mutation.type)
    if type_name is None:
        raise InvalidParamsError("type required")
    if mutation.type is not None and mutation.type != type_name:
        raise ConflictError("type is immutable")
    return existing, type_name


def _relation_endpoints(
    connection: apsw.Connection,
    mutation: RelationWrite,
    nodes: list[dict[str, Any] | None],
    existing: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve explicit creation refs or the immutable persisted endpoints."""
    if existing is not None:
        source = (
            _prepared_ref(connection, mutation.source_ref, nodes)
            if mutation.source_ref is not None
            else row_by_id(connection, "nodes", existing["source_id"])
        )
        target = (
            _prepared_ref(connection, mutation.target_ref, nodes)
            if mutation.target_ref is not None
            else row_by_id(connection, "nodes", existing["target_id"])
        )
    else:
        source = _prepared_ref(connection, mutation.source_ref, nodes)
        target = _prepared_ref(connection, mutation.target_ref, nodes)
    if source is None or target is None:
        raise NotFoundError("relation endpoint missing")
    if existing is not None and (source["id"] != existing["source_id"] or target["id"] != existing["target_id"]):
        raise ConflictError("endpoints are immutable")
    return source, target


def _deduplicate_relation(
    connection: apsw.Connection,
    mutation: RelationWrite,
    existing: dict[str, Any] | None,
    type_name: str,
    source: dict[str, Any],
    target: dict[str, Any],
    properties: dict[str, Any],
    key: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Match one endpoint-bound relation identity and merge nonidentity properties."""
    if existing is not None:
        current = json.loads(existing["properties"])
        if identity_json("relations", type_name, current) != identity_json("relations", type_name, properties):
            raise ConflictError("identity properties are immutable")
        return existing, properties
    if source["id"] < 0 or target["id"] < 0:
        return None, properties
    identifier = connection.execute(
        "SELECT id FROM relations WHERE source_id=? AND type=? AND target_id=? AND key=?",
        (source["id"], type_name, target["id"], key),
    ).get
    if identifier is None:
        return None, properties
    existing = row_by_id(connection, "relations", identifier)
    if existing is None:
        raise NotFoundError("record disappeared")
    require_ready(connection, "relations", existing)
    stored = json.loads(existing["properties"])
    if identity_json("relations", type_name, stored) != identity_json("relations", type_name, properties):
        raise ConflictError("identity hash collision")
    properties = merge_properties(stored, mutation.properties, mutation.remove_properties)
    validate_record("relations", type_name, properties)
    return existing, properties


def _prepare_relation(
    connection: apsw.Connection,
    mutation: RelationWrite,
    nodes: list[dict[str, Any] | None],
    synthetic_id: int,
) -> _PreparedMutation:
    """Resolve and validate a relation only after every node identity is known."""
    existing, type_name = _relation_header(connection, mutation)
    source, target = _relation_endpoints(connection, mutation, nodes, existing)
    _validate_endpoints(type_name, source, target)
    _enforce_single_parent(connection, type_name, source, target)
    current: dict[str, Any] = json.loads(existing["properties"]) if existing is not None else {}
    properties = merge_properties(current, mutation.properties, mutation.remove_properties)
    validate_record("relations", type_name, properties)
    key = identity_key("relations", type_name, properties)
    existing, properties = _deduplicate_relation(
        connection, mutation, existing, type_name, source, target, properties, key
    )
    require_ready(connection, "nodes", source)
    require_ready(connection, "nodes", target)
    row = _preflight_row(existing, type_name, key, properties, synthetic_id)
    return _PreparedMutation(mutation, type_name, existing, row, properties, endpoints=(source, target))


def _preflight_links(connection: apsw.Connection, plans: list[_PreparedMutation]) -> None:
    """Resolve all evidence IDs before any canonical row is inserted or updated."""
    for plan in plans:
        for identifier in (*plan.mutation.evidence_add, *plan.mutation.evidence_remove):
            evidence = row_by_id(connection, "evidence", identifier)
            if evidence is None:
                raise NotFoundError("evidence reference missing")
            require_ready(connection, "evidence", evidence)


def _store_node_plan(
    connection: apsw.Connection,
    token: OperationToken,
    request: WriteRequest,
    headers: list[tuple[dict[str, Any] | None, str]],
    node_plans: list[_PreparedMutation | None],
    node_rows: list[dict[str, Any] | None],
    seen: set[tuple[str, str]],
    index: int,
    parent: dict[str, Any] | None = None,
) -> None:
    """Prepare and index one node while rejecting duplicate request identities."""
    token.check()
    mutation = request.nodes[index]
    existing, type_name = headers[index]
    plan = _prepare_node(connection, mutation, existing, type_name, parent, -(index + 1))
    identity = (plan.type_name, cast("str", plan.row["key"]))
    if identity in seen:
        raise InvalidParamsError("duplicate batch identity")
    seen.add(identity)
    node_plans[index] = plan
    node_rows[index] = plan.row


def _prepare_node_plans(
    connection: apsw.Connection, token: OperationToken, request: WriteRequest
) -> tuple[list[_PreparedMutation], list[dict[str, Any] | None]]:
    """Resolve unscoped nodes first, followed by the catalog scope dependency order."""
    headers = [_node_header(connection, mutation) for mutation in request.nodes]
    node_plans: list[_PreparedMutation | None] = [None] * len(request.nodes)
    node_rows: list[dict[str, Any] | None] = [None] * len(request.nodes)
    seen_nodes: set[tuple[str, str]] = set()
    for index, (_existing, type_name) in enumerate(headers):
        if _scope(type_name) is None:
            _store_node_plan(connection, token, request, headers, node_plans, node_rows, seen_nodes, index)
    for scoped_type in _SCOPED_NODE_ORDER:
        for index, (existing, type_name) in enumerate(headers):
            if type_name != scoped_type:
                continue
            if existing is not None:
                _store_node_plan(connection, token, request, headers, node_plans, node_rows, seen_nodes, index)
                continue
            relation = _scope_relation_for_node(index, type_name, request.relations)
            parent = _prepared_ref(connection, relation.source_ref, node_rows)
            require_ready(connection, "nodes", parent)
            _store_node_plan(connection, token, request, headers, node_plans, node_rows, seen_nodes, index, parent)
    if any(plan is None for plan in node_plans):
        raise InvalidParamsError("scoped node dependency could not be resolved")
    return cast("list[_PreparedMutation]", node_plans), node_rows


def _prepare_relation_plans(
    connection: apsw.Connection,
    token: OperationToken,
    request: WriteRequest,
    node_rows: list[dict[str, Any] | None],
) -> list[_PreparedMutation]:
    """Validate all relation endpoints and endpoint-bound identities."""
    relation_plans: list[_PreparedMutation] = []
    seen_relations: set[tuple[str, str, str, str]] = set()
    for index, mutation in enumerate(request.relations):
        token.check()
        plan = _prepare_relation(connection, mutation, node_rows, -(index + 1))
        source, target = plan.endpoints
        if source is None or target is None:
            raise InvalidParamsError("endpoints required")
        identity = (
            cast("str", source["uuid"]),
            plan.type_name,
            cast("str", target["uuid"]),
            cast("str", plan.row["key"]),
        )
        if identity in seen_relations:
            raise InvalidParamsError("duplicate batch identity")
        seen_relations.add(identity)
        relation_plans.append(plan)
    return relation_plans


def _preflight(
    connection: apsw.Connection, token: OperationToken, request: WriteRequest
) -> tuple[list[_PreparedMutation], list[_PreparedMutation]]:
    """Resolve the complete graph batch and all identities before persistence."""
    node_plans, node_rows = _prepare_node_plans(connection, token, request)
    relation_plans = _prepare_relation_plans(connection, token, request, node_rows)
    _preflight_links(connection, [*node_plans, *relation_plans])
    return node_plans, relation_plans


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
    parent_id: str | None = None,
    planned_uuid: str | None = None,
) -> tuple[dict[str, Any], bool]:
    source, target = endpoints
    type_name = row["type"] if row else mutation.type
    if type_name is None:
        raise InvalidParamsError("type required")
    key = identity_key(kind, type_name, properties, parent_id)
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
        identifier = planned_uuid or str(uuid4())
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
        """Preflight the complete parent-scoped batch, then persist it under the existing write lock."""
        output: dict[str, Any] = {"nodes": [], "relations": []}
        try:
            node_plans, relation_plans = _preflight(connection, token, request)
            for kind, plans in (("nodes", node_plans), ("relations", relation_plans)):
                for plan in plans:
                    token.check()
                    row, created = _persist(
                        connection,
                        kind,
                        plan.mutation,
                        plan.existing,
                        plan.properties,
                        plan.endpoints,
                        plan.parent_id,
                        cast("str", plan.row["uuid"]),
                    )
                    plan.row.clear()
                    plan.row.update(row)
                    added, removed = _links(connection, kind, row, plan.mutation)
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
        except ExpectedValidationError as exc:
            raise InvalidParamsError(exc.message) from None
        except ValueError:
            raise InvalidParamsError("invalid graph mutation") from None
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
    """Discover catalog definitions, including identity properties/scope, and ready-only counts."""
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
