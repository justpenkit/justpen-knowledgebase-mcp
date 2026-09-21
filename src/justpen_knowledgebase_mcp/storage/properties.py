"""Transactional property projection owned by canonical graph mutations."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ..catalog import catalog_view
from ..indexing import flatten_properties
from ..mutations import canonical_json
from .graph_sql import PROPERTY_DELETE, PROPERTY_INSERT

if TYPE_CHECKING:
    import apsw


def refresh_properties(connection: apsw.Connection, kind: str, row: dict[str, Any], properties: dict[str, Any]) -> None:
    """Replace all derived paths and coverage within the caller's transaction."""
    required = {
        "/" + field.replace("~", "~0").replace("/", "~1") for field in catalog_view()[kind][row["type"]]["required"]
    }
    projection = flatten_properties(properties, required_paths=required)
    connection.execute(PROPERTY_DELETE[kind], (row["id"],))
    connection.executemany(
        PROPERTY_INSERT[kind],
        [
            (row["id"], path, item.value_type, int(item.value_materialized), item.value)
            for path, item in projection.rows.items()
        ],
    )
    metadata = json.loads(row["metadata"])
    metadata["property_index"] = projection.metadata()
    row["metadata"] = canonical_json(metadata)
    connection.execute(
        {"nodes": "UPDATE nodes SET metadata=? WHERE id=?", "relations": "UPDATE relations SET metadata=? WHERE id=?"}[
            kind
        ],
        (row["metadata"], row["id"]),
    )
