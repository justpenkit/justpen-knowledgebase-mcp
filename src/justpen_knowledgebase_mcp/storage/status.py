"""One deadline-bound status sample executed by an existing reader owner."""

from __future__ import annotations

import json
from contextlib import closing
from typing import TYPE_CHECKING, Any

import apsw

from ..errors import LimitError
from .fulltext import coverage

if TYPE_CHECKING:
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
        "nodes": "SELECT count(*) FROM nodes INDEXED BY nodes_property_fallback WHERE lifecycle='ready' AND coalesce(json_extract(metadata,'$.property_index.complete'),0)!=1",
        "relations": "SELECT count(*) FROM relations r INDEXED BY relations_property_fallback CROSS JOIN nodes s ON s.id=r.source_id CROSS JOIN nodes t ON t.id=r.target_id WHERE r.lifecycle='ready' AND s.lifecycle='ready' AND t.lifecycle='ready' AND coalesce(json_extract(r.metadata,'$.property_index.complete'),0)!=1",
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


DERIVED_OBJECTS = {
    "text_projection_bytes": ("search_documents",),
    "fts_index_bytes": ("search_fts_data", "search_fts_idx", "search_fts_docsize", "search_fts_config"),
    "property_index_bytes": (
        "node_property_index",
        "relation_property_index",
        "nodes_property_fallback",
        "relations_property_fallback",
    ),
}


# `dbstat` reads every page of every object it reports. Measured at 3.4-3.6 us
# per page across 4751 to 888443 pages, so the sampler's one-second budget buys
# roughly 289000 pages. Past this bound the walk cannot finish on any host and
# only wastes a reader thread before the budget cancels it mid-page, so the same
# LimitError is raised up front instead. `PRAGMA page_count` reads the header in
# 0.012 ms and bounds the walk from above: it counts the whole database, never
# fewer pages than the selected objects hold.
DBSTAT_PAGE_LIMIT = 262144


def sample_derived_storage(connection: apsw.Connection, token: OperationToken) -> dict[str, Any]:
    """Sum selected B-tree pages with cancellation between pages and no text materialization."""
    sizes = dict.fromkeys(DERIVED_OBJECTS, 0)
    try:
        if connection.execute("PRAGMA page_count").get > DBSTAT_PAGE_LIMIT:
            raise LimitError("derived storage sample exceeds the page allocation budget")
        for category, names in DERIVED_OBJECTS.items():
            for object_name in names:
                token.check()
                with closing(
                    connection.execute(
                        "SELECT name FROM sqlite_schema WHERE (tbl_name=? OR name=?) AND type IN ('table','index')",
                        (object_name, object_name),
                    )
                ) as objects:
                    for (name,) in objects:
                        with closing(connection.execute("SELECT pgsize FROM dbstat WHERE name=?", (name,))) as pages:
                            for (page_size,) in pages:
                                token.check()
                                sizes[category] += page_size
    except apsw.SQLError:
        token.check()
        return {"available": False, "reason": "DBSTAT_UNAVAILABLE"}
    token.check()
    return {"available": True, "reason": None, **sizes}
