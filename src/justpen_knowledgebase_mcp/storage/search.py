"""Bounded live ID selection using SQL evidence and canonical fallback."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ..cursors import CursorBinding
from ..errors import InvalidParamsError, LimitError
from ..identity import parse_timestamp
from ..models import SearchResult
from ..mutations import canonical_json
from ..query import compile_filter, evaluate
from .graph_sql import PROPERTY_BODY, READY, SEARCH_CANDIDATE

if TYPE_CHECKING:
    import apsw

    from ..models import SearchRequest
    from .worker import OperationToken


def search(connection: apsw.Connection, token: OperationToken, request: SearchRequest) -> dict[str, Any]:
    """Resolve each candidate before advancing; never skip unknown owners."""
    workspace, epoch = connection.execute("SELECT workspace_id,query_epoch FROM settings WHERE singleton=1").get
    binding = CursorBinding(
        workspace, epoch, request.kind, None, "search", request.model_dump(exclude={"cursor", "limit"})
    )
    try:
        after = binding.decode(request.cursor) if request.cursor else 0
    except ValueError as exc:
        raise InvalidParamsError("invalid cursor") from exc
    expression, values = (
        compile_filter(request.properties, request.kind) if request.properties is not None else ("1", [])
    )
    clauses, filters = _builtin_filters(request)
    sql = SEARCH_CANDIDATE[request.kind].format(expression=expression, conditions=" AND ".join(clauses))
    output: dict[str, Any] = {
        "results": [],
        "next_cursor": None,
        "has_more": False,
        "property_filter_mode": "index_only",
        "canonical_scan_count": 0,
        "incomplete": False,
    }
    last_returned = after
    while True:
        token.check()
        candidate = connection.execute(sql, [*values, after, *filters]).fetchone()
        if candidate is None:
            break
        identifier, uuid, type_name, key, metadata, answer = candidate
        if answer is None:
            token.check()
            body = connection.execute(PROPERTY_BODY[request.kind], (identifier,)).get
            output["canonical_scan_count"] += 1
            output["property_filter_mode"] = "canonical_fallback"
            answer = evaluate(json.loads(body), request.properties or {})
            token.check()
        if answer:
            item = {"id": uuid, "type": type_name, "key": key, **json.loads(metadata)}
            if (
                len(output["results"]) == request.limit
                or len(canonical_json({**output, "results": [*output["results"], item]}).encode("utf-8")) > 245000
            ):
                if not output["results"]:
                    raise LimitError("search item exceeds response budget")
                output["has_more"] = True
                output["next_cursor"] = binding.encode(last_returned)
                break
            output["results"].append(item)
            last_returned = identifier
        after = identifier
    token.check()
    return SearchResult.model_validate(output).model_dump(exclude_unset=True)


def _builtin_filters(request: SearchRequest) -> tuple[list[str], list[Any]]:
    clauses = [READY[request.kind], "o.id>?"]
    filters: list[Any] = []
    for field in ("type", "key"):
        value = getattr(request, field)
        if value is not None:
            clauses.append({"type": "o.type=?", "key": "o.key=?"}[field])
            filters.append(value)
    if request.source is not None:
        clauses.append("json_extract(o.metadata,'$.source')=?")
        filters.append(request.source)
    for field in ("source_id", "target_id"):
        value = getattr(request, field)
        if value is not None:
            clauses.append(
                {
                    "source_id": "o.source_id=(SELECT id FROM nodes WHERE uuid=?)",
                    "target_id": "o.target_id=(SELECT id FROM nodes WHERE uuid=?)",
                }[field]
            )
            filters.append(value)
    for timestamp, clause in (
        (request.observed_at_min, "o.observed_at>=?"),
        (request.observed_at_max, "o.observed_at<=?"),
    ):
        if timestamp is not None:
            clauses.append(clause)
            filters.append(parse_timestamp(timestamp))
    return clauses, filters
