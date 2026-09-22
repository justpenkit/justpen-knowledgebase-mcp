# MCP tool reference

The server registers exactly 11 tools:

| Tool                                                   | Purpose                                                               |
| ------------------------------------------------------ | --------------------------------------------------------------------- |
| [`kb_status`](maintenance.md#kb_status)                | Cached health, WAL, queue, retention, index, scope, and fixed limits. |
| [`kb_types`](graph.md#kb_types)                        | Discover fixed node/relation schemas and ready counts.                |
| [`kb_write`](graph.md#kb_write)                        | Atomically create/upsert/patch graph records and evidence links.      |
| [`kb_get`](graph.md#kb_get)                            | Read records, evidence links, or evidence sources.                    |
| [`kb_search`](graph.md#kb_search)                      | Search graph/evidence summaries with exact filters and text.          |
| [`kb_neighbors`](graph.md#kb_neighbors)                | Traverse ready stored relations with explicit budgets.                |
| [`kb_delete`](maintenance.md#kb_delete)                | Admit durable graph/evidence deletion.                                |
| [`kb_ingest_evidence`](evidence.md#kb_ingest_evidence) | Store exact path/text/base64 bytes and index eligible text.           |
| [`kb_read_evidence`](evidence.md#kb_read_evidence)     | Read a bounded raw byte range.                                        |
| [`kb_jobs`](maintenance.md#kb_jobs)                    | List, inspect, cancel, or retry eligible durable jobs.                |
| [`kb_reindex`](maintenance.md#kb_reindex)              | Rebuild derived indexes from canonical records/evidence.              |

## Common MCP contract

Inputs are closed and strict: unknown fields and type coercion are rejected.
Most lists accept at most 100 values. Successful calls return
`{"status":"ok","data":...}`. Expected application failures return
`{"status":"error","error":"CODE: message"}` and may include one bounded,
typed `details` object. Public codes are `INVALID`, `NOT_FOUND`, `CONFLICT`,
`BUSY`, `LIMIT`, `PATH_DENIED`, `IO_ERROR`, `INDEX_ERROR`, `CANCELLED`,
`CONFIGURATION`, and `INTERNAL`.

FastMCP validates each call against the tool's published input schema before the
request model is built. Such a rejection carries the same `INVALID` envelope,
naming the rejected argument paths and the rules they broke without repeating
the values sent; raw NaN/Infinity is one example. A call that reaches no tool at
all, such as an unknown tool name or a protocol frame the transport cannot
parse, fails before any envelope exists, so clients should also handle an MCP
`is_error` result without `structured_content`.

A record identifier is refused whichever layer catches it. The published schema
promises the 36-byte width and the canonical lowercase 8-4-4-4-12 spelling as an
anchored `pattern`, so a host can refuse `550E8400-...` without a round trip. An
evidence identifier publishes its own `e_` plus 64 lowercase hex `pattern`. The
patterns are metadata: the server still checks on arrival and answers
`INVALID: invalid tool request; <argument>: non-canonical graph id` as the
envelope above, so a client that skips the schema check loses nothing but a
round trip.

Serialized success envelopes are capped at 256 KiB. A single result that cannot
fit returns `LIMIT`; list operations otherwise return explicit remainder,
pagination, or truncation state.

## Pagination and live state

`kb_types`, association/source `kb_get`, ID-sorted `kb_search`, and list
`kb_jobs` use opaque bound cursors. The default page is 20 and maximum is 100.
Cursors bind the workspace/query epoch and selection, but are not authorization
tokens and do not hold a database snapshot. Do not change filters while reusing
one. Concurrent pending deletion may make a graph temporarily disconnected or
skip a pending row between pages.

Record-view `kb_get` uses `remaining_ids` instead of a cursor because callers
already supplied the ordered IDs. Relevance search has no cursor and returns the
best 100 candidates at most. `kb_neighbors` uses `truncated`, `reason`, and
`frontier` rather than a continuation cursor.

The [graph guide](../guides/graph.md), [evidence/search guide](../guides/evidence-search.md),
and [workspace operations guide](../guides/workspace.md) explain the data and
lifecycle contracts behind these fields.
