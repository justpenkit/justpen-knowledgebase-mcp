"""Mutable graph primitives inside an existing admitted worker transaction."""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from ..catalog import (
    EndpointView,
    catalog_manifest,
    catalog_schema,
    catalog_view,
    check_endpoint_values,
    format_descriptions,
    inventory_description,
    registrable_domain,
    rule_descriptions,
    scope_order,
    type_description,
    validate_record,
)
from ..cursors import CursorBinding
from ..errors import (
    ConflictError,
    ExpectedValidationError,
    InvalidParamsError,
    LimitError,
    NotFoundError,
    RecordConflictError,
    RejectedIdentityError,
)
from ..identity import format_timestamp, identity_json, identity_key, parse_timestamp
from ..models import GetRequest, Mutation, NodeRef, NodeWrite, RelationWrite, WriteRequest, WriteResult
from ..mutations import canonical_json, merge_properties, validate_properties
from ..responses import BlockerDetails, RejectedIdentityDetails
from . import fulltext, graph_sql as sql
from .inventory import effective_state
from .properties import refresh_properties

if TYPE_CHECKING:
    import apsw

    from ..models import TypesRequest
    from .worker import OperationToken


KINDS = frozenset(("nodes", "relations", "evidence"))
# Derived parent-first from the catalog: a scoped type that is itself a scope-relation source
# must be resolved before its child, and deriving it keeps that true without a manual reorder.
_SCOPED_NODE_ORDER = scope_order()
# `Graph.get` counts its whole record output, so its remainder below the 262 121-byte serialized
# data bound of `bounded_response` is only the response envelope. `_associations` counts its items
# alone, leaving its remainder to carry the view key and a 4096-character `next_cursor`.
RECORD_RESPONSE_BYTES = 250000
ASSOCIATION_RESPONSE_BYTES = 245000
# How preflight matched a node: a new row, the row an ID addresses, or a row an identity upsert hit.
MatchKind = Literal["created", "id", "identity"]
# The inventory columns of a node before any claim applies; `allowlist_scoped` is NOT NULL.
_NO_STATE: Mapping[str, Any] = MappingProxyType(
    {
        "ownership": None,
        "authorization": None,
        "allowlist_scoped": 0,
        "authorization_override": None,
        "state_root_uuid": None,
    }
)
_CLAIMS = frozenset((*sql.OWNERSHIP_CLAIMS, *sql.AUTHORIZATION_CLAIMS))
# An owned or dependency node relying on a candidate through one of these makes it a dependency,
# so its rejection is refused. Containment and discovery relations are purged with the rejection.
_RELIANCE_RELATIONS = json.dumps(
    sorted(
        (
            "backed_by_bucket",
            "cname_to",
            "dname_to",
            "federates_with",
            "has_mail_exchange",
            "has_nameserver",
            "has_soa_primary",
            "has_srv_target",
            "has_svcb_binding",
            "hosted_on",
            "resolves_to",
        )
    )
)
# How far past the server's clock an `observed_at` may run, in microseconds: last seen keeps the
# maximum, so a later one would outrank every real scan until the clock caught up with it.
_OBSERVATION_SKEW = 300_000_000


def _member_bytes(member: object) -> int:
    # An array member costs its own canonical bytes plus one separator, so n members are charged
    # `sum(len) + n`. What that is measured against differs by caller. `Graph.get` starts from the
    # serialized empty envelope, whose brackets are already counted, so it over-counts each
    # non-empty array by exactly one byte and never under-counts its own output. `_associations`
    # counts members alone, one byte below the array's own `sum(len) + n + 1`; its budget reserves
    # the whole view key and cursor beside that array, so the byte is never the one that decides.
    return len(canonical_json(member).encode("utf-8")) + 1


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
    match: MatchKind | None = None
    # The observation time the write reports, or None when it reports none.
    observed: int | None = None
    # Whether this write moves the node to `rejected`, which strips it and purges its subtree.
    rejecting: bool = False


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


def _observation_time(mutation: Mutation) -> int:
    """A write observes at its `observed_at`, or at the current time when it omits one."""
    return parse_timestamp(mutation.observed_at) if mutation.observed_at is not None else time.time_ns() // 1000


def _refuse_future_observations(request: WriteRequest) -> None:
    """Refuse an `observed_at` later than the server's current time plus the clock-skew tolerance."""
    limit = time.time_ns() // 1000 + _OBSERVATION_SKEW
    for kind, mutations in (("nodes", request.nodes), ("relations", request.relations)):
        for index, mutation in enumerate(mutations):
            if mutation.observed_at is not None and parse_timestamp(mutation.observed_at) > limit:
                raise InvalidParamsError(
                    f"{kind}[{index}]: observed_at {mutation.observed_at} is later than the server's current time"
                )


def _reported(mutation: Mutation, existing: dict[str, Any] | None, observed: int) -> int | None:
    """Creations, identity rescans, `observed_at` and property changes observe; other ID writes do not."""
    if (
        existing is None
        or mutation.id is None
        or mutation.observed_at is not None
        or mutation.properties
        or mutation.remove_properties
    ):
        return observed
    return None


def _merge_observed(
    current: dict[str, Any], mutation: Mutation, stored: dict[str, Any] | None, observed: int
) -> dict[str, Any]:
    """Merge a write onto a record; one observed before the record's last seen only adds what it lacks."""
    if stored is None or stored["last_seen"] is None or observed >= stored["last_seen"]:
        return merge_properties(current, mutation.properties, mutation.remove_properties)
    result = copy.deepcopy(current)
    _add_missing(result, mutation.properties)
    validate_properties(result)
    return result


def _add_missing(target: dict[str, Any], patch: dict[str, Any]) -> None:
    """Add each patch key the target lacks, at any object depth, without replacing a stored value."""
    for key, value in patch.items():
        if key not in target:
            target[key] = copy.deepcopy(value)
        elif type(value) is dict and type(target[key]) is dict:
            _add_missing(cast("dict[str, Any]", target[key]), cast("dict[str, Any]", value))


def _identity_definition(kind: str, type_name: str) -> Mapping[str, Any]:
    """Return one shared read-only identity declaration from the fixed catalog."""
    return cast("Mapping[str, Any]", catalog_view()[kind][type_name]["identity"])


def _scope(type_name: str) -> Mapping[str, str] | None:
    """Return the parent-scope declaration for a node type, when present."""
    value = _identity_definition("nodes", type_name).get("scope")
    return cast("Mapping[str, str]", value) if isinstance(value, Mapping) else None


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
    """Gate the endpoint types and self edges; value checks wait for the merged properties."""
    definition = catalog_view()["relations"].get(type_name)
    if definition is None or source["type"] not in definition["sources"] or target["type"] not in definition["targets"]:
        raise InvalidParamsError("relation endpoint types are not allowed")
    if source["id"] == target["id"] and not definition["self_edge"]:
        raise InvalidParamsError("self edge is not allowed")


def _validate_endpoint_values(
    type_name: str, properties: dict[str, Any], source: dict[str, Any], target: dict[str, Any]
) -> None:
    """Run the catalog's endpoint value checks against the properties that will be stored."""
    check_endpoint_values(
        type_name,
        properties,
        EndpointView(cast("str", source["type"]), json.loads(source["properties"])),
        EndpointView(cast("str", target["type"]), json.loads(target["properties"])),
    )


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
    definition = catalog_view()["nodes"].get(type_name)
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
    observed: int,
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
    properties = _merge_observed(stored, mutation, existing, observed)
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
    observed = _observation_time(mutation)
    current: dict[str, Any] = json.loads(existing["properties"]) if existing is not None else {}
    properties = _merge_observed(current, mutation, existing, observed)
    validate_record("nodes", type_name, properties)
    key = identity_key("nodes", type_name, properties, parent_id)
    addressed = existing is not None
    existing, properties = _deduplicate_node(
        connection, mutation, existing, type_name, properties, parent_id, key, observed
    )
    row = _preflight_row(existing, type_name, key, properties, synthetic_id)
    match: MatchKind = "id" if addressed else "identity" if existing is not None else "created"
    if existing is None:
        row.update(_NO_STATE, state_root_uuid=_state_root(parent))
    return _PreparedMutation(
        mutation,
        type_name,
        existing,
        row,
        properties,
        parent_id,
        match=match,
        observed=_reported(mutation, existing, observed),
    )


def _state_root(parent: dict[str, Any] | None) -> str | None:
    """Fix a new scoped node's state root from its parent's planned row; nodes persist in request order."""
    if parent is None:
        return None
    if catalog_view()["nodes"][parent["type"]]["inventory"] == "carries":
        return cast("str", parent["uuid"])
    return cast("str | None", parent["state_root_uuid"])


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
    observed: int,
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
    properties = _merge_observed(stored, mutation, existing, observed)
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
    observed = _observation_time(mutation)
    current: dict[str, Any] = json.loads(existing["properties"]) if existing is not None else {}
    properties = _merge_observed(current, mutation, existing, observed)
    validate_record("relations", type_name, properties)
    key = identity_key("relations", type_name, properties)
    existing, properties = _deduplicate_relation(
        connection, mutation, existing, type_name, source, target, properties, key, observed
    )
    # After both merges: `_deduplicate_relation` re-merges onto an edge the write matched by key.
    _validate_endpoint_values(type_name, properties, source, target)
    require_ready(connection, "nodes", source)
    require_ready(connection, "nodes", target)
    row = _preflight_row(existing, type_name, key, properties, synthetic_id)
    return _PreparedMutation(
        mutation,
        type_name,
        existing,
        row,
        properties,
        endpoints=(source, target),
        observed=_reported(mutation, existing, observed),
    )


def _preflight_links(connection: apsw.Connection, plans: list[_PreparedMutation]) -> None:
    """Resolve all evidence IDs before any canonical row is inserted or updated."""
    for plan in plans:
        for identifier in (*plan.mutation.evidence_add, *plan.mutation.evidence_remove):
            evidence = row_by_id(connection, "evidence", identifier)
            if evidence is None:
                raise NotFoundError("evidence reference missing")
            require_ready(connection, "evidence", evidence)


def _refuse_rejected_identities(connection: apsw.Connection, plans: list[_PreparedMutation]) -> None:
    """Collect every item that would re-create a rejected identity, then refuse once.

    This runs before relation planning, so a scanner batch learns every rejected name in one error
    rather than first meeting an edge of one of them that a purge job has not yet removed.
    """
    domains: dict[str, str | None] = {}
    hits = [
        {"item": f"nodes[{index}]", "rejected_record": rejected}
        for index, plan in enumerate(plans)
        if (rejected := _rejected_identity(connection, plan, domains)) is not None
    ]
    if hits:
        raise RejectedIdentityError(RejectedIdentityDetails.model_validate({"rejected_items": hits}))


def _rejected_identity(
    connection: apsw.Connection, plan: _PreparedMutation, domains: dict[str, str | None]
) -> str | None:
    """Name the rejected record an identity match hits, or the rejected domain a new subdomain sits under.

    `domains` keeps each registrable domain's answer for the batch, so its subdomains share one lookup.
    """
    if plan.match == "identity" and plan.existing is not None and plan.existing["ownership"] == "rejected":
        return cast("str", plan.existing["uuid"])
    if plan.match != "created" or plan.type_name != "subdomain":
        return None
    domain = registrable_domain(cast("str", plan.properties["value"]))
    if domain is None:
        return None
    if domain not in domains:
        row = connection.execute(
            sql.NODE_STATE_BY_KEY, ("domain", identity_key("nodes", "domain", {"value": domain}))
        ).fetchone()
        domains[domain] = cast("str", row[0]) if row is not None and row[1] == "rejected" else None
    return domains[domain]


def _preflight_state(
    connection: apsw.Connection, plans: list[_PreparedMutation], relation_plans: list[_PreparedMutation]
) -> None:
    """Apply the inventory rules to every node's planned post-write state before any row is written.

    Refusals name the item address, because the rule concerns one item of a batch the agent built.
    """
    for index, plan in enumerate(plans):
        address = f"nodes[{index}]"
        mutation = cast("NodeWrite", plan.mutation)
        inventory = catalog_view()["nodes"][plan.type_name]["inventory"]
        if inventory == "carries":
            _plan_carried_state(address, plan, mutation)
        elif inventory == "inherits":
            _plan_scoped_state(address, plan, mutation)
        else:
            for field in ("ownership", "authorization", "allowlist_scoped"):
                if getattr(mutation, field) is not None:
                    raise InvalidParamsError(f"{address}: {plan.type_name} carries no inventory state; omit {field}")
        _require_remaining_evidence(connection, address, plan)
    planned = {cast("str", plan.row["uuid"]): plan.row for plan in plans}
    for index, plan in enumerate(plans):
        _require_allowlist_root(connection, f"nodes[{index}]", plan, planned)
        if plan.rejecting:
            _refuse_relied_upon(connection, plan, planned)
    _refuse_subdomains_of_planned_rejections(plans)
    _refuse_rejected_subtrees(connection, plans, relation_plans, planned)


def _refuse_subdomains_of_planned_rejections(plans: list[_PreparedMutation]) -> None:
    """Refuse a new subdomain under a domain this batch rejects, as one under a stored rejection is."""
    rejected = {
        cast("str", plan.properties["value"]): cast("str", plan.row["uuid"])
        for plan in plans
        if plan.rejecting and plan.type_name == "domain"
    }
    hits = [
        {"item": f"nodes[{index}]", "rejected_record": rejected[domain]}
        for index, plan in enumerate(plans)
        if plan.match == "created"
        and plan.type_name == "subdomain"
        and (domain := registrable_domain(cast("str", plan.properties["value"]))) is not None
        and domain in rejected
    ]
    if hits:
        raise RejectedIdentityError(RejectedIdentityDetails.model_validate({"rejected_items": hits}))


def _refuse_rejected_subtrees(
    connection: apsw.Connection,
    plans: list[_PreparedMutation],
    relation_plans: list[_PreparedMutation],
    planned: dict[str, dict[str, Any]],
) -> None:
    """A rejected node, planned or stored, keeps no scoped descendants and no relations."""
    stored: dict[str, dict[str, Any] | None] = {}
    for index, plan in enumerate(plans):
        if _scope(plan.type_name) is not None and _under_rejection(connection, plan.row, planned, stored):
            raise ConflictError(
                f"nodes[{index}]: the {plan.type_name} is scoped under a rejected record, which keeps no descendants"
            )
    for index, plan in enumerate(relation_plans):
        for side, endpoint in zip(("source", "target"), plan.endpoints, strict=True):
            if endpoint is not None and _under_rejection(connection, endpoint, planned, stored):
                raise ConflictError(
                    f"relations[{index}]: the {side} {endpoint['type']} is rejected or scoped under a rejected "
                    "record, which keeps no relations"
                )


def _plan_carried_state(address: str, plan: _PreparedMutation, mutation: NodeWrite) -> None:
    """Apply creation state or the ownership transition table; an identity match keeps its stored state."""
    if plan.match == "identity":
        return
    row = plan.row
    evidenced = bool(mutation.evidence_add)
    if plan.match == "created":
        if mutation.ownership is None:
            raise InvalidParamsError(
                f"{address}: ownership is required to create a {plan.type_name}; give owned, dependency or candidate"
            )
        row["authorization"] = "unknown"
    if mutation.ownership is not None and mutation.ownership != row["ownership"]:
        _check_ownership_transition(address, plan.type_name, row["ownership"], mutation.ownership, evidenced=evidenced)
        row["ownership"] = mutation.ownership
        if mutation.ownership == "rejected":
            _plan_rejection(plan)
    _plan_root_authorization(address, plan, mutation, evidenced=evidenced)


def _plan_rejection(plan: _PreparedMutation) -> None:
    """Keep the identity and the other required properties, and reset authorization.

    A rejection is not an observation, so first and last seen stay as they were.
    """
    definition = catalog_view()["nodes"][plan.type_name]
    kept = {*definition["identity"]["properties"], *definition["required"]}
    plan.properties = {key: value for key, value in plan.properties.items() if key in kept}
    validate_record("nodes", plan.type_name, plan.properties)
    plan.row.update(
        properties=canonical_json(plan.properties),
        authorization="unknown",
        allowlist_scoped=0,
        authorization_override=None,
    )
    plan.observed = None
    plan.rejecting = True


def _refuse_relied_upon(
    connection: apsw.Connection, plan: _PreparedMutation, planned: dict[str, dict[str, Any]]
) -> None:
    """An owned or dependency node that relies on the candidate makes it a dependency; name the relation."""
    for relation, source, stored in connection.execute(sql.RELIANCE_SOURCES, (plan.row["id"], _RELIANCE_RELATIONS)):
        ownership = planned[source]["ownership"] if source in planned else stored
        if ownership in ("owned", "dependency"):
            raise RecordConflictError(
                "REJECTION_BLOCKED",
                BlockerDetails.model_validate({"blocking_record": {"kind": "relations", "id": relation}}),
            )


def _under_rejection(
    connection: apsw.Connection,
    row: dict[str, Any],
    planned: dict[str, dict[str, Any]],
    stored: dict[str, dict[str, Any] | None],
) -> bool:
    """Whether a node is rejected, or scoped under a root that is, in the planned post-write state.

    `stored` keeps the stored roots already read for the batch, which preflight does not change.
    """
    root_id = cast("str", row["state_root_uuid"] or row["uuid"])
    root = planned.get(root_id) or (row if root_id == row["uuid"] else None)
    if root is None:
        if root_id not in stored:
            stored[root_id] = row_by_id(connection, "nodes", root_id)
        root = stored[root_id]
    return root is not None and root["ownership"] == "rejected"


def _plan_root_authorization(address: str, plan: _PreparedMutation, mutation: NodeWrite, *, evidenced: bool) -> None:
    """Apply a carrying node's authorization and allowlist marker, which only an in_scope node may hold."""
    row = plan.row
    if mutation.authorization is not None and mutation.authorization != row["authorization"]:
        if row["ownership"] == "rejected":
            raise ConflictError(f"{address}: authorization cannot change on a rejected {plan.type_name}")
        if mutation.authorization != "unknown" and not evidenced:
            raise InvalidParamsError(
                f"{address}: setting authorization to {mutation.authorization} requires evidence_add in the same write"
            )
        row["authorization"] = mutation.authorization
    if mutation.allowlist_scoped is not None and mutation.allowlist_scoped != bool(row["allowlist_scoped"]):
        if not evidenced:
            raise InvalidParamsError(f"{address}: changing allowlist_scoped requires evidence_add in the same write")
        row["allowlist_scoped"] = int(mutation.allowlist_scoped)
    if row["allowlist_scoped"] and row["authorization"] != "in_scope":
        raise ConflictError(
            f"{address}: allowlist_scoped requires authorization in_scope, and the {plan.type_name} "
            f"would be {row['authorization']}"
        )


def _check_ownership_transition(
    address: str, type_name: str, current: str | None, target: str, *, evidenced: bool
) -> None:
    """Refuse an ownership change the transition table forbids, naming the current state."""
    if target == "rejected":
        if current is None:
            raise InvalidParamsError(f"{address}: a new {type_name} cannot be created as rejected")
        if current != "candidate":
            raise ConflictError(
                f"{address}: ownership cannot move from {current} to rejected; withdraw to candidate first"
            )
    elif target == "candidate":
        if current == "rejected":
            raise ConflictError(f"{address}: ownership cannot move from rejected to candidate")
        return
    if not evidenced:
        change = (
            f"creating a {type_name} as {target}" if current is None else f"moving ownership from {current} to {target}"
        )
        raise InvalidParamsError(f"{address}: {change} requires evidence_add in the same write")


def _plan_scoped_state(address: str, plan: _PreparedMutation, mutation: NodeWrite) -> None:
    """Refuse state a scoped child inherits, and apply its authorization override on creation or by ID."""
    for field in ("ownership", "allowlist_scoped"):
        if getattr(mutation, field) is not None:
            raise InvalidParamsError(
                f"{address}: a {plan.type_name} inherits its ownership and allowlist from its state root; omit {field}"
            )
    if plan.match == "identity" or mutation.authorization is None:
        return
    override = None if mutation.authorization == "unknown" else mutation.authorization
    if override == plan.row["authorization_override"]:
        return
    if override is not None and not mutation.evidence_add:
        raise InvalidParamsError(
            f"{address}: overriding a {plan.type_name}'s authorization to {override} "
            "requires evidence_add in the same write"
        )
    plan.row["authorization_override"] = override


def _require_allowlist_root(
    connection: apsw.Connection, address: str, plan: _PreparedMutation, planned: dict[str, dict[str, Any]]
) -> None:
    """Admit a newly set in_scope override only under a root whose planned state is allowlist-scoped."""
    if plan.row["authorization_override"] != "in_scope":
        return
    if plan.existing is not None and plan.existing["authorization_override"] == "in_scope":
        return
    root_id = cast("str", plan.row["state_root_uuid"])
    root = planned.get(root_id) or row_by_id(connection, "nodes", root_id)
    if root is None or not root["allowlist_scoped"]:
        raise ConflictError(
            f"{address}: an in_scope override needs an allowlist-scoped root, and this {plan.type_name}'s root is not"
        )


def _require_remaining_evidence(connection: apsw.Connection, address: str, plan: _PreparedMutation) -> None:
    """Refuse a write that leaves a claim-holding node without a link to ready evidence."""
    mutation = plan.mutation
    if not mutation.evidence_remove:
        return
    row = plan.row
    claim = next(
        (
            value
            for value in (row["ownership"], row["authorization"], row["authorization_override"])
            if value in _CLAIMS
        ),
        None,
    )
    if claim is None or set(mutation.evidence_add) - set(mutation.evidence_remove):
        return
    if (
        plan.existing is not None
        and connection.execute(sql.READY_LINK_KEPT, (row["id"], json.dumps(mutation.evidence_remove))).get
    ):
        return
    raise ConflictError(
        f"{address}: the {plan.type_name} holds a {claim} claim, and this write would remove its last ready evidence"
    )


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
    _refuse_future_observations(request)
    node_plans, node_rows = _prepare_node_plans(connection, token, request)
    _refuse_rejected_identities(connection, node_plans)
    relation_plans = _prepare_relation_plans(connection, token, request, node_rows)
    _preflight_links(connection, [*node_plans, *relation_plans])
    _preflight_state(connection, node_plans, relation_plans)
    return node_plans, relation_plans


def _persist(
    connection: apsw.Connection,
    kind: str,
    mutation: Mutation,
    row: dict[str, Any] | None,
    properties: dict[str, Any],
    endpoints: tuple[dict[str, Any] | None, dict[str, Any] | None],
    parent_id: str | None = None,
    planned_uuid: str | None = None,
    state: Mapping[str, Any] | None = None,
    observed: int | None = None,
    *,
    clear_label: bool = False,
) -> tuple[dict[str, Any], bool]:
    source, target = endpoints
    type_name = row["type"] if row else mutation.type
    if type_name is None:
        raise InvalidParamsError("type required")
    key = identity_key(kind, type_name, properties, parent_id)
    now = time.time_ns() // 1000
    metadata = _metadata(kind, mutation, row, clear_label=clear_label)
    first_seen, last_seen = _seen(row, observed)
    created = row is None
    if row is None:
        identifier = planned_uuid or str(uuid4())
        if kind == "nodes":
            planned = state or _NO_STATE
            connection.execute(
                "INSERT INTO nodes(uuid,type,key,properties,metadata,created_at,updated_at,first_seen,last_seen,"
                "ownership,authorization,allowlist_scoped,authorization_override,state_root_uuid) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    type_name,
                    key,
                    canonical_json(properties),
                    canonical_json(metadata),
                    now,
                    now,
                    first_seen,
                    last_seen,
                    *(planned[column] for column in _NO_STATE),
                ),
            )
        else:
            if source is None or target is None:
                raise InvalidParamsError("endpoints required")
            connection.execute(
                "INSERT INTO relations(uuid,source_id,type,target_id,key,properties,metadata,created_at,updated_at,"
                "first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
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
                    first_seen,
                    last_seen,
                ),
            )
        row = row_by_id(connection, kind, identifier)
        if row is None:
            raise NotFoundError("record disappeared")
    else:
        connection.execute(
            sql.OWNER_UPDATE[kind],
            (canonical_json(properties), canonical_json(metadata), now, first_seen, last_seen, row["id"]),
        )
        _update_state(connection, row, state)
        row = row_by_id(connection, kind, row["id"])
        if row is None:
            raise NotFoundError("record disappeared")
    refresh_properties(connection, kind, row, properties)
    fulltext.refresh_record_text(connection, kind, row)
    return row, created


def _metadata(kind: str, mutation: Mutation, row: dict[str, Any] | None, *, clear_label: bool) -> dict[str, Any]:
    """Apply the write's source and label to the stored metadata; a rejection clears the label."""
    metadata: dict[str, Any] = json.loads(row["metadata"]) if row else {"source": None}
    if kind == "nodes":
        metadata.setdefault("label", None)
    for field in ("source", "label"):
        if field in mutation.model_fields_set:
            metadata[field] = getattr(mutation, field)
    if clear_label:
        metadata["label"] = None
    return metadata


def _seen(row: dict[str, Any] | None, observed: int | None) -> tuple[int | None, int | None]:
    """Fold an observation into first seen as a minimum and into last seen as a maximum."""
    first, last = (row["first_seen"], row["last_seen"]) if row is not None else (None, None)
    if observed is None:
        return first, last
    return (observed if first is None else min(first, observed), observed if last is None else max(last, observed))


def _update_state(connection: apsw.Connection, row: dict[str, Any], state: Mapping[str, Any] | None) -> None:
    """Write the node state an ID write changed; the state root never changes after creation."""
    columns = sql.NODE_STATE_COLUMNS
    if state is not None and any(state[column] != row[column] for column in columns):
        connection.execute(sql.NODE_STATE_UPDATE, (*(state[column] for column in columns), row["id"]))


def _mark_rejection(connection: apsw.Connection, row: dict[str, Any]) -> str:
    """Hide a rejected node's relations and scoped descendants under one purge job's intent.

    Records already pending under another deletion keep that job; the caller admits this one.
    """
    job_id = str(uuid4())
    intent = (job_id, 1, time.time_ns() // 1000)
    connection.execute(sql.REJECTION_DESCENDANTS_PENDING, (*intent, row["uuid"]))
    for query in sql.REJECTION_INCIDENT_PENDING:
        connection.execute(query, (*intent, row["id"]))
    return job_id


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
        """Preflight the complete parent-scoped batch, then persist it under the existing write lock.

        A rejection marks its subtree pending under the `rejection_job_id` it reports; the caller
        admits that job in the same transaction (`JobRunner.write`).
        """
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
                        {column: plan.row[column] for column in _NO_STATE} if kind == "nodes" else None,
                        plan.observed,
                        clear_label=plan.rejecting,
                    )
                    plan.row.clear()
                    plan.row.update(row)
                    added, removed = _links(connection, kind, row, plan.mutation)
                    item: dict[str, Any] = {
                        "id": row["uuid"],
                        "created": created,
                        "updated": not created,
                        "links_added": added,
                        "links_removed": removed,
                        "property_index": json.loads(row["metadata"])["property_index"],
                    }
                    if plan.rejecting:
                        item["rejection_job_id"] = _mark_rejection(connection, row)
                    output[kind].append(item)
            # After every row and scope relation exists: the acknowledgement reports effective state.
            for plan, item in zip(node_plans, output["nodes"], strict=True):
                state = effective_state(connection, plan.row)
                if state is not None:
                    item.update(ownership=state.ownership, authorization=state.authorization)
        except ExpectedValidationError as exc:
            raise InvalidParamsError(exc.message) from None
        except ValueError:
            raise InvalidParamsError("invalid graph mutation") from None
        return WriteResult.model_validate(output).model_dump(exclude_unset=True)

    @staticmethod
    def get(connection: apsw.Connection, token: OperationToken, request: GetRequest) -> dict[str, Any]:
        """Return whole records with explicit missing and response-budget remainder."""
        if request.view != "record":
            return _associations(connection, request)
        output: dict[str, Any] = {"records": [], "missing_ids": [], "remaining_ids": []}
        response_bytes = len(canonical_json(output).encode("utf-8"))
        for identifier in request.ids:
            token.check()
            row = row_by_id(connection, request.kind, identifier)
            if row is None:
                output["missing_ids"].append(identifier)
                response_bytes += _member_bytes(identifier)
                continue
            record = _record(connection, request.kind, row)
            cost = _member_bytes(record)
            if response_bytes + cost > RECORD_RESPONSE_BYTES:
                output["remaining_ids"].append(identifier)
                response_bytes += _member_bytes(identifier)
                continue
            output["records"].append(record)
            response_bytes += cost
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
        for field in ("first_seen", "last_seen"):
            record[field] = format_timestamp(row[field]) if row[field] is not None else None
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
    # A purge deletes a node's scope relation before the node, so only a ready chain is walked.
    if kind == "nodes" and blocker is None:
        _record_state(connection, record, row)
    record["lifecycle"] = "delete_pending" if blocker else "ready"
    record["delete_job_id"] = blocker["delete_job_id"] if blocker else None
    record["pending_since"] = blocker["pending_since"] if blocker else None
    return record


def _record_state(connection: apsw.Connection, record: dict[str, Any], row: dict[str, Any]) -> None:
    """Add effective state: a root's allowlist marker, or the root a scoped node inherits from."""
    state = effective_state(connection, row)
    if state is None:
        return
    record.update(state.fields())
    if state.root_id is None:
        record["allowlist_scoped"] = bool(row["allowlist_scoped"])


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
    item_bytes = 0
    for index, row in enumerate(rows):
        cost = _member_bytes(row[1])
        if index == request.limit or item_bytes + cost > ASSOCIATION_RESPONSE_BYTES:
            # A first item over the budget leaves no position to encode: an evidence `links` cursor
            # carries the association kind of the last returned item, and there is none.
            if not items:
                raise LimitError("association item exceeds response budget")
            truncated = True
            break
        item_bytes += cost
        last = row[0]
        if request.kind == "evidence" and request.view == "links":
            last_kind = row[1]["kind"]
        items.append(row[1])
    return {request.view: items, "next_cursor": binding.encode(last, last_kind) if truncated else None}


def graph_types(connection: apsw.Connection, token: OperationToken, request: TypesRequest) -> dict[str, Any]:
    """Discover catalog definitions, identity properties/scope, inventory state, and ready-only counts."""
    # Deliberately not the shared read view: this hands `common`, `formats` and every definition
    # straight into the response, so it needs a tree it owns. It is also one call per request rather
    # than several per record, so the parse it keeps is not the cost the shared view exists to save.
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
    descriptions = rule_descriptions()
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
        definition = definitions[name]
        rules = [*definition["checks"], *definition["canonicalize"]]
        items.append(
            {
                "type": name,
                **definition,
                "description": type_description(request.kind, name),
                "check_descriptions": {rule_id: descriptions[rule_id] for rule_id in rules},
                "properties_schema": catalog_schema(request.kind, name),
                "count": count,
            }
        )
    return {
        "types": items,
        "counts_deferred": deferred,
        "common": manifest["common"],
        "formats": format_descriptions(),
        "inventory": inventory_description(),
        "next_cursor": binding.encode(after + len(items)) if after + len(items) < len(names) else None,
    }
