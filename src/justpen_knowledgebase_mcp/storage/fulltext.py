"""Transactional FTS projections and bounded verified match materialization."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..errors import ConflictError, NotFoundError
from ..mutations import canonical_json
from ..text import TextChunk, record_text_units, tokens
from . import graph

if TYPE_CHECKING:
    from collections.abc import Iterator

    import apsw

    from ..query import TextQuery
    from .worker import OperationToken

OWNER_COLUMN = {"nodes": "node_id", "relations": "relation_id", "evidence": "evidence_id"}


def refresh_record_text(connection: apsw.Connection, kind: str, row: dict[str, Any]) -> None:
    """Replace full canonical string leaves within the graph mutation transaction."""
    connection.execute(DOCUMENT_DELETE[kind], (row["id"],))
    connection.executemany(
        DOCUMENT_INSERT[kind],
        ((row["id"], pointer, text) for pointer, text in record_text_units(row)),
    )


def coverage(connection: apsw.Connection) -> dict[str, int]:
    """Read current ready-owner evidence coverage from this search snapshot."""
    result: dict[str, int] = dict.fromkeys(("ready", "pending", "failed", "incomplete", "not_applicable"), 0)
    for state, incomplete, count in connection.execute(
        "SELECT index_state,incomplete,count(*) FROM evidence WHERE lifecycle='ready' GROUP BY index_state,incomplete"
    ):
        result["failed" if state == "index_failed" else state] += count
        if incomplete:
            result["incomplete"] += count
    return result


def _literal_ranges(tokenizer: apsw.FTS5Tokenizer, text: str, query: TextQuery) -> Iterator[tuple[int, int]]:
    data, needle = text.encode("utf-8"), query.original.encode("utf-8")
    offsets = tokens(tokenizer, data)
    starts, ends = {x[0] for x in offsets}, {x[1] for x in offsets}
    after = 0
    while (start := data.find(needle, after)) >= 0:
        end = start + len(needle)
        if start + query.token_start in starts and start + query.token_end in ends:
            yield len(data[:start].decode("utf-8")), len(data[:end].decode("utf-8"))
        after = start + 1


def _reference(
    document: tuple[Any, ...], kind: str, uuid: str, start: int, end: int
) -> tuple[dict[str, Any], str, bool, dict[str, int]] | None:
    _id, pointer, text, byte_start, line_start, owner_start, encoding, previous_cr, _rank = document
    chunk = TextChunk(text, encoding or "utf-8", byte_start or 0, line_start or 1, owner_start or 0, bool(previous_cr))
    first, last, line_first, line_last = chunk.source_range(start, end)
    if kind == "evidence" and last <= chunk.owner_start:
        return None
    snippet = text[start:].encode("utf-8")[:512].decode("utf-8", errors="ignore")
    ref = {
        "kind": kind,
        "id": uuid,
        "pointer": pointer,
        "byte_start": first,
        "byte_end": last,
        "line_start": line_first,
        "line_end": line_last,
    }
    snippet_range: dict[str, int] = dict(
        zip(
            ("byte_start", "byte_end", "line_start", "line_end"),
            chunk.source_range(start, start + len(snippet)),
            strict=True,
        )
    )
    return ref, snippet, end - start > len(snippet), snippet_range


class _Matches:
    """Retain only bounded references and one snippet for a winning unit."""

    def __init__(self) -> None:
        self.refs: list[dict[str, Any]] = []
        self.seen: set[tuple[str | None, int, int]] = set()
        self.snippet: str | None = None
        self.snippet_range: dict[str, int] | None = None
        self.truncated = False
        self.snippet_truncated = False
        self.snippet_source: tuple[str, str, str | None] | None = None
        self.reference_bytes = 0

    def add(self, document: tuple[Any, ...], reference: tuple[dict[str, Any], str, bool, dict[str, int]]) -> None:
        ref, snippet, cut, snippet_range = reference
        key = (document[1], ref["byte_start"], ref["byte_end"])
        if key in self.seen:
            return
        cost = len(canonical_json(ref).encode("utf-8"))
        if len(self.refs) < 32 and (not self.refs or self.reference_bytes + cost <= 100000):
            self.seen.add(key)
            self.refs.append(ref)
            self.reference_bytes += cost
        else:
            self.truncated = True
        if self.snippet is None:
            self.snippet, self.snippet_truncated = snippet, cut
            self.snippet_range = snippet_range
            self.snippet_source = (ref["kind"], ref["id"], ref["pointer"])
        elif self.snippet_range is not None and (
            (ref["kind"], ref["id"], ref["pointer"]) != self.snippet_source
            or ref["byte_start"] < self.snippet_range["byte_start"]
            or ref["byte_end"] > self.snippet_range["byte_end"]
        ):
            self.snippet_truncated = True

    def result(self, query: TextQuery, score: float) -> dict[str, Any]:
        return {
            "score": score,
            "query_mode": query.mode,
            "snippet": self.snippet,
            "snippet_range": self.snippet_range,
            "snippet_truncated": self.snippet_truncated,
            "matches": self.refs,
            "matches_truncated": self.truncated,
        }


def _document_ranges(
    tokenizer: apsw.FTS5Tokenizer, text: str, query: TextQuery, expression_index: int
) -> Iterator[tuple[int, int]]:
    if query.mode == "literal":
        yield from _literal_ranges(tokenizer, text, query)
    else:
        data = text.encode("utf-8")
        for a, b, word in tokens(tokenizer, data):
            if word == query.tokens[expression_index]:
                yield len(data[:a].decode("utf-8")), len(data[:b].decode("utf-8"))


def match_unit(
    connection: apsw.Connection, token: OperationToken, kind: str, identifier: int, uuid: str, query: TextQuery
) -> dict[str, Any] | None:
    """Literal verifies one leaf; words combines token scores only within this unit."""
    tokenizer = connection.fts5_tokenizer("unicode61")
    matches = _Matches()
    score = 0.0
    for index, expression in enumerate(query.expressions):
        best: float | None = None
        for document in connection.execute(DOCUMENT_MATCH[kind], (expression, identifier)):
            token.check()
            for start, end in _document_ranges(tokenizer, document[2], query, index):
                token.check()
                reference = _reference(document, kind, uuid, start, end)
                if reference is None:
                    continue
                rank = float(document[-1])
                best = rank if best is None else min(best, rank)
                matches.add(document, reference)
                # Later hits in this document cannot change its rank or a fully
                # saturated result. A byte-budget truncation alone is insufficient:
                # a later shorter pointer may still fill unoccupied reference slots.
                if len(matches.refs) == 32 and matches.truncated and matches.snippet_truncated:
                    break
            token.check()
        if best is None:
            return None
        score += best
    return matches.result(query, score)


def owner_match(
    connection: apsw.Connection,
    token: OperationToken,
    kind: str,
    identifier: int,
    uuid: str,
    query: TextQuery,
    *,
    include_evidence: bool,
) -> dict[str, Any] | None:
    """Select the best complete record or single attached-evidence match unit."""
    best = match_unit(connection, token, kind, identifier, uuid, query)
    if kind != "evidence" and include_evidence:
        for evidence_id, evidence_uuid in connection.execute(EVIDENCE_UNITS[kind], (identifier,)):
            token.check()
            candidate = match_unit(connection, token, "evidence", evidence_id, evidence_uuid, query)
            if candidate is not None and (best is None or candidate["score"] < best["score"]):
                best = candidate
    return best


@dataclass(frozen=True)
class IndexOwner:
    """Immutable item-level capability in addition to the durable job lease."""

    identifier: int
    uuid: str
    generation: int
    job_id: int
    token: str
    encoding: str
    media_type: str
    byte_size: int


def index_owner_active(connection: apsw.Connection, owner: int | None) -> bool:
    """Queued reservations and live claims retain item ownership across steps."""
    return (
        owner is not None
        and connection.execute(
            "SELECT 1 FROM jobs WHERE id=? AND (state='queued' OR (state='running' AND lease_expires_at>?))",
            (owner, time.time()),
        ).get
        is not None
    )


def claim_item(connection: apsw.Connection, uuid: str, job_id: int, claim_token: str) -> IndexOwner:
    """Claim a ready item unless another unexpired job actively owns this generation."""
    row = connection.execute(
        "SELECT id,index_generation,index_owner_job_id,encoding,media_type,lifecycle,byte_size FROM evidence WHERE uuid=?",
        (uuid,),
    ).fetchone()
    if row is None:
        raise NotFoundError("evidence not found")
    identifier, generation, owner, encoding, media_type, lifecycle, byte_size = row
    if lifecycle != "ready":
        pending = graph.row_by_id(connection, "evidence", uuid)
        if pending is None:
            raise NotFoundError("evidence not found")
        graph.require_ready(connection, "evidence", pending)
    if owner != job_id and index_owner_active(connection, owner):
        # The job ID reserves the item across queued/reclaimed lease transitions.
        # The item token fences writes only; it may still belong to an older attempt.
        raise ConflictError("INDEX_BUSY")
    connection.execute(
        "UPDATE evidence SET index_owner_job_id=?,index_owner_token=?,index_state='pending',incomplete=1 WHERE id=?",
        (job_id, claim_token, identifier),
    )
    return IndexOwner(identifier, uuid, generation, job_id, claim_token, encoding, media_type, byte_size)


def check_item(connection: apsw.Connection, owner: IndexOwner) -> None:
    """Fence every derived cleanup, batch, finish and release against current metadata."""
    valid = connection.execute(
        "SELECT 1 FROM evidence WHERE id=? AND lifecycle='ready' AND index_generation=? AND index_owner_job_id=? AND index_owner_token=?",
        (owner.identifier, owner.generation, owner.job_id, owner.token),
    ).get
    if valid is None:
        raise ConflictError("INDEX_GENERATION_CHANGED")


def clear_item_batch(connection: apsw.Connection, owner: IndexOwner) -> bool:
    """Delete at most 100 derived canonical rows with atomic FTS trigger maintenance."""
    check_item(connection, owner)
    connection.execute(
        "DELETE FROM search_documents WHERE id IN (SELECT id FROM search_documents WHERE evidence_id=? ORDER BY id LIMIT 100)",
        (owner.identifier,),
    )
    return connection.changes() == 0


def append_chunk(connection: apsw.Connection, owner: IndexOwner, chunk: TextChunk) -> None:
    """Publish a materialized bounded chunk only under the expected generation/owner."""
    check_item(connection, owner)
    if not chunk.gap:
        connection.execute(
            "INSERT INTO search_documents(evidence_id,text,byte_start,byte_end,line_start,line_end,overlap_owner,index_generation,encoding,previous_cr) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                owner.identifier,
                chunk.text,
                chunk.byte_start,
                chunk.byte_end,
                chunk.line_start,
                chunk.source_range(0, len(chunk.text))[3],
                chunk.owner_start,
                owner.generation,
                chunk.encoding,
                int(chunk.previous_cr),
            ),
        )


def finish_item(connection: apsw.Connection, owner: IndexOwner, state: str, *, incomplete: bool) -> None:
    """Update coverage and release only this item's expected capability."""
    check_item(connection, owner)
    connection.execute(
        "UPDATE evidence SET index_state=?,incomplete=?,index_owner_job_id=NULL,index_owner_token=NULL WHERE id=? AND index_generation=? AND index_owner_job_id=? AND index_owner_token=?",
        (state, int(incomplete), owner.identifier, owner.generation, owner.job_id, owner.token),
    )


_DELETE_TEMPLATE = "DELETE FROM search_documents WHERE {owner}=?"
DOCUMENT_DELETE = {kind: _DELETE_TEMPLATE.format(owner=owner) for kind, owner in OWNER_COLUMN.items()}
DOCUMENT_INSERT = {
    kind: f"INSERT INTO search_documents({owner},pointer,text,byte_start,line_start,encoding,overlap_owner) VALUES(?,?,?,0,1,'utf-8',0)"
    for kind, owner in OWNER_COLUMN.items()
}
_MATCH_TEMPLATE = "SELECT d.id,d.pointer,d.text,d.byte_start,d.line_start,d.overlap_owner,d.encoding,d.previous_cr,bm25(search_fts) FROM search_fts JOIN search_documents d ON d.id=search_fts.rowid WHERE search_fts MATCH ? AND d.{owner}=?"
_EVIDENCE_GENERATION = " AND EXISTS(SELECT 1 FROM evidence e WHERE e.id=d.evidence_id AND e.lifecycle='ready' AND e.index_state!='not_applicable' AND e.index_generation=d.index_generation)"
DOCUMENT_MATCH = {
    kind: "".join((_MATCH_TEMPLATE.format(owner=owner), _EVIDENCE_GENERATION if kind == "evidence" else ""))
    for kind, owner in OWNER_COLUMN.items()
}

EVIDENCE_UNITS = {
    "nodes": "SELECT e.id,e.uuid FROM node_evidence l JOIN evidence e ON e.id=l.evidence_id WHERE l.node_id=? AND e.lifecycle='ready' ORDER BY e.id",
    "relations": "SELECT e.id,e.uuid FROM relation_evidence l JOIN evidence e ON e.id=l.evidence_id WHERE l.relation_id=? AND e.lifecycle='ready' ORDER BY e.id",
}
