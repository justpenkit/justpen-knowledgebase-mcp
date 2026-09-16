"""Bounded live ID selection using SQL evidence and canonical fallback."""

from __future__ import annotations

import json
from contextlib import closing
from typing import TYPE_CHECKING, Any

from ..cursors import CursorBinding
from ..errors import InvalidParamsError, LimitError
from ..identity import parse_timestamp
from ..models import SearchResult
from ..mutations import canonical_json
from ..query import compile_filter, compile_text_query, evaluate
from ..responses import bounded_response
from .fulltext import coverage, owner_match
from .graph_sql import PROPERTY_BODY, READY, SEARCH_CANDIDATE

if TYPE_CHECKING:
    from collections.abc import Generator

    import apsw

    from ..models import SearchRequest
    from ..query import TextQuery
    from .worker import OperationToken


def search(connection: apsw.Connection, token: OperationToken, request: SearchRequest) -> dict[str, Any]:
    """Resolve each candidate before advancing; never skip unknown owners."""
    workspace, epoch = connection.execute("SELECT workspace_id,query_epoch FROM settings WHERE singleton=1").get
    binding = CursorBinding(
        workspace, epoch, request.kind, None, "search", request.model_dump(exclude={"cursor", "limit"})
    )
    try:
        after = binding.decode(request.cursor) if request.cursor is not None else 0
    except ValueError as exc:
        raise InvalidParamsError("invalid cursor") from exc
    text_query = (
        compile_text_query(request.query, request.query_mode, tokenizer=connection.fts5_tokenizer("unicode61"))
        if request.query is not None
        else None
    )
    expression, values = (
        compile_filter(request.properties, request.kind) if request.properties is not None else ("1", [])
    )
    clauses, filters = _builtin_filters(request)
    if text_query is not None:
        candidate_sql = TEXT_CANDIDATE[request.kind]
        filters.append(text_query.expressions[0])
        if request.kind != "evidence" and request.include_evidence:
            candidate_sql = " UNION ".join((candidate_sql, LINKED_CANDIDATE[request.kind]))
            filters.append(text_query.expressions[0])
        clauses.append(f"o.id IN ({candidate_sql})")
    sql = (
        "SELECT o.id,o.uuid,NULL,NULL,json_object('media_type',o.media_type,'index_state',o.index_state,'byte_size',o.byte_size),1 FROM evidence o WHERE {conditions} ORDER BY o.id"
        if request.kind == "evidence"
        else SEARCH_CANDIDATE[request.kind]
    ).format(expression=expression, conditions=" AND ".join(clauses))
    current_coverage = coverage(connection)
    output: dict[str, Any] = {
        "items": [],
        "cursor": None,
        "has_more": False,
        "property_filter_mode": "index_only",
        "canonical_scan_count": 0,
        "incomplete": bool(current_coverage["incomplete"]),
        "coverage": current_coverage,
    }
    last_returned = after
    ranked: list[tuple[float, int, dict[str, Any]]] = []
    ranked_count = 0
    with closing(
        _matched_candidates(connection, token, request, text_query, sql, values, filters, after, output)
    ) as candidates:
        for identifier, item in candidates:
            if request.sort == "relevance":
                ranked_count += 1
                ranked.append((item["score"], identifier, item))
                ranked.sort(key=lambda entry: (entry[0], entry[1]))
                del ranked[request.limit :]
                continue
            if (
                len(output["items"]) == request.limit
                or len(canonical_json({**output, "items": [*output["items"], item]}).encode("utf-8")) > 245000
            ):
                if not output["items"]:
                    raise LimitError("search item exceeds response budget")
                output["has_more"] = True
                output["cursor"] = binding.encode(last_returned)
                break
            output["items"].append(item)
            last_returned = identifier
    token.check()
    if request.sort == "relevance":
        _ranked_output(output, ranked, ranked_count)
    return bounded_response(SearchResult.model_validate(output).model_dump(exclude_unset=True))


def _builtin_filters(request: SearchRequest) -> tuple[list[str], list[Any]]:
    clauses = ["o.lifecycle='ready'" if request.kind == "evidence" else READY[request.kind], "o.id>?"]
    filters: list[Any] = []
    for field in ("type", "key"):
        value = getattr(request, field)
        if value is not None:
            clauses.append({"type": "o.type=?", "key": "o.key=?"}[field])
            filters.append(value)
    if request.source is not None:
        clauses.append(
            "EXISTS(SELECT 1 FROM evidence_sources es WHERE es.evidence_id=o.id AND es.source=?)"
            if request.kind == "evidence"
            else "json_extract(o.metadata,'$.source')=?"
        )
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
    for field, clause in (
        ("media_type", "o.media_type=?"),
        ("index_state", "o.index_state=?"),
        ("byte_size_min", "o.byte_size>=?"),
        ("byte_size_max", "o.byte_size<=?"),
        ("created_at_min", "o.created_at>=?"),
        ("created_at_max", "o.created_at<=?"),
    ):
        value = getattr(request, field)
        if value is not None:
            clauses.append(clause)
            filters.append(parse_timestamp(value) if field.startswith("created_at") else value)
    return clauses, filters


def _matched_candidates(
    connection: apsw.Connection,
    token: OperationToken,
    request: SearchRequest,
    text_query: TextQuery | None,
    sql: str,
    values: list[Any],
    filters: list[Any],
    after: int,
    output: dict[str, Any],
) -> Generator[tuple[int, dict[str, Any]], None, None]:
    with closing(connection.execute(sql, [*values, after, *filters])) as candidates:
        for candidate in candidates:
            token.check()
            identifier, uuid, type_name, key, metadata, answer = candidate
            if answer is None:
                body = connection.execute(PROPERTY_BODY[request.kind], (identifier,)).get
                output["canonical_scan_count"] += 1
                output["property_filter_mode"] = "canonical_fallback"
                answer = evaluate(json.loads(body), request.properties or {})
                token.check()
            if not answer:
                continue
            match = (
                owner_match(
                    connection,
                    token,
                    request.kind,
                    identifier,
                    uuid,
                    text_query,
                    include_evidence=request.include_evidence,
                )
                if text_query is not None
                else {}
            )
            if match is None:
                continue
            item = {"id": uuid, **json.loads(metadata), **match}
            if request.kind != "evidence":
                item.update(type=type_name, key=key)
            yield identifier, item


def _ranked_output(output: dict[str, Any], ranked: list[tuple[float, int, dict[str, Any]]], ranked_count: int) -> None:
    output["has_more"] = ranked_count > len(ranked)
    for _, _, item in ranked:
        if len(canonical_json({**output, "items": [*output["items"], item]}).encode("utf-8")) > 245000:
            output["has_more"] = True
            break
        output["items"].append(item)


_TEXT_CANDIDATE = (
    "SELECT d.{owner} FROM search_fts JOIN search_documents d ON d.id=search_fts.rowid WHERE search_fts MATCH ?"
)
TEXT_CANDIDATE = {
    kind: _TEXT_CANDIDATE.format(owner=owner)
    for kind, owner in {"nodes": "node_id", "relations": "relation_id", "evidence": "evidence_id"}.items()
}
LINKED_CANDIDATE = {
    "nodes": "SELECT l.node_id FROM search_fts JOIN search_documents d ON d.id=search_fts.rowid JOIN node_evidence l ON l.evidence_id=d.evidence_id WHERE search_fts MATCH ?",
    "relations": "SELECT l.relation_id FROM search_fts JOIN search_documents d ON d.id=search_fts.rowid JOIN relation_evidence l ON l.evidence_id=d.evidence_id WHERE search_fts MATCH ?",
}
