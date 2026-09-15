"""One deadline-bound status sample executed by an existing reader owner."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .fulltext import coverage

if TYPE_CHECKING:
    import apsw

    from .worker import OperationToken


def sample_status(connection: apsw.Connection, token: OperationToken) -> dict[str, Any]:
    """Materialize bounded aggregate metadata, never raw records or paths."""
    row = connection.execute(
        "SELECT schema_version,catalog_version,index_format_version,policy FROM settings WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise ValueError("missing settings")
    schema, catalog, index, policy = row
    counts = dict.fromkeys(("queued", "running", "completed", "failed", "cancelled"), 0)
    for state, count in connection.execute("SELECT state,count(*) FROM jobs GROUP BY state"):
        token.check()
        counts[state] = count
    fallback: dict[str, int] = {}
    queries = {
        "nodes": "SELECT count(*) FROM nodes WHERE lifecycle='ready' AND coalesce(json_extract(metadata,'$.property_index.complete'),0)!=1",
        "relations": "SELECT count(*) FROM relations r JOIN nodes s ON s.id=r.source_id JOIN nodes t ON t.id=r.target_id WHERE r.lifecycle='ready' AND s.lifecycle='ready' AND t.lifecycle='ready' AND coalesce(json_extract(r.metadata,'$.property_index.complete'),0)!=1",
    }
    for kind, query in queries.items():
        token.check()
        fallback[kind] = connection.execute(query).get
    token.check()
    return {
        "schema_version": schema,
        "catalog_version": catalog,
        "index_format_version": index,
        "policy": json.loads(policy),
        "jobs": counts,
        "index_coverage": coverage(connection),
        "property_index_fallback": fallback,
    }
