"""Effective inventory state: a carrying root's own state, narrowed or widened along its scope chain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..catalog import scope_relations
from ..errors import ConflictError

if TYPE_CHECKING:
    from collections.abc import Sequence

    import apsw

_SCOPE_RELATIONS = scope_relations()
# A scope chain never repeats a scoped type, so it is at most this many links above its node.
_SCOPE_DEPTH = len(_SCOPE_RELATIONS)
# Only the columns the walk reads: each hop's link and override, and the root's state.
_SCOPE_PARENT = (
    "SELECT parent.id,parent.uuid,parent.type,parent.ownership,parent.authorization,parent.allowlist_scoped,"
    "parent.authorization_override FROM relations r JOIN nodes parent ON parent.id=r.source_id "
    "WHERE r.target_id=? AND r.type=? ORDER BY r.id LIMIT 1"
)


@dataclass(frozen=True)
class EffectiveState:
    """The ownership and authorization a node reports, and the root it inherits them from."""

    ownership: str
    authorization: str
    root_id: str | None

    def fields(self) -> dict[str, str]:
        """The state a read reports: ownership, authorization and, under a root, that root's ID."""
        fields = {"ownership": self.ownership, "authorization": self.authorization}
        if self.root_id is not None:
            fields["state_root_id"] = self.root_id
        return fields


def effective_authorization(root: dict[str, Any], overrides: Sequence[str | None]) -> str:
    """Resolve authorization from the root and the overrides of the scoped nodes below it.

    `out_of_scope` anywhere on the chain wins. Under an allowlist-scoped root a scoped node is
    `in_scope` only when it or an ancestor below the root was widened; otherwise it takes the root's.
    """
    if root["authorization"] == "out_of_scope" or "out_of_scope" in overrides:
        return "out_of_scope"
    if overrides and root["allowlist_scoped"]:
        return "in_scope" if "in_scope" in overrides else "out_of_scope"
    return str(root["authorization"])


def effective_state(connection: apsw.Connection, row: dict[str, Any]) -> EffectiveState | None:
    """Return a node's effective state, or None for a type that carries no inventory state."""
    root_id = row["state_root_uuid"]
    if root_id is None:
        if row["ownership"] is None:
            return None
        return EffectiveState(row["ownership"], row["authorization"], None)
    overrides: list[str | None] = []
    current = row
    for _ in range(_SCOPE_DEPTH):
        overrides.append(current["authorization_override"])
        cursor = connection.execute(_SCOPE_PARENT, (current["id"], _SCOPE_RELATIONS[current["type"]]))
        values = cursor.fetchone()
        if values is None:
            raise ConflictError("scoped node must have exactly one parent relation")
        current = dict(zip([item[0] for item in cursor.get_description()], values, strict=True))
        if current["uuid"] == root_id:
            return EffectiveState(current["ownership"], effective_authorization(current, overrides), root_id)
    raise ConflictError("scope chain does not reach its state root")
