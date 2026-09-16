# Graph and search tools

## `kb_types`

**Input:** `kind` is `nodes` or `relations`; optional `type`; `limit` defaults to
20 (1–100); optional cursor.

**Output:** catalog entries with required property schemas, identity fields,
formats/enums/cross-field rules, and ready-only counts, plus common formats,
`counts_deferred`, and `next_cursor`. A pending high-degree delete can defer
counts rather than block discovery.

**Errors:** `INVALID` for a bad kind/type/page/cursor; `LIMIT` or `BUSY` for
bounded admission. Listing types is discovery and does not replace `kb_status`.

## `kb_write`

**Input:** 1–100 total `nodes` plus `relations`, and at most 100 total
`evidence_add`/`evidence_remove` mutations. Creation supplies `type` and
`properties`; patch supplies `id`. Relations also supply immutable
`source_ref`/`target_ref`, each exactly one `{id}` or same-batch `{node_index}`.

**Output:** ordered node/relation acknowledgments with UUID, created/updated
flags, link counts, and property-index coverage.

**Errors:** `INVALID` for strict catalog, merge, pointer, endpoint, or batch-rule
failure; `NOT_FOUND` for an atomic missing reference/evidence set;
`CONFLICT`/`RECORD_DELETING` for pending records; `BUSY`, `LIMIT`, or storage
errors. No partial batch commits.

Object patches merge recursively, arrays replace whole values, and `{}` leaves
existing object children. Required identity cannot change. Explicit `null`
clears label/source but is literal data inside properties. A supplied
`observed_at` is last-writer-wins rather than maximum timestamp.

## `kb_get`

**Input:** `kind`, 1–100 IDs, `view` (`record` default, `links`, or `sources`),
`limit` 20 by default, and optional cursor. Association views take one owner;
`sources` is evidence-only; record view rejects a cursor.

**Output:** record view returns canonical records, `missing_ids`, and
`remaining_ids`. Links/sources return a page plus `next_cursor`. Evidence records
include lifecycle/index state; pending lifecycle remains inspectable.

**Errors:** `INVALID` for mismatched IDs/view/cursor; `RECORD_DELETING` for a
pending links/source page; `LIMIT`, `BUSY`, or storage errors. Missing record-view
IDs are data, not an all-or-nothing error.

## `kb_search`

**Input:** required `kind`; optional literal/words `query`; `sort` defaults to
`id`; graph type/key/source/endpoint/timestamp/property filters; evidence
media/index/size/created/source filters; `limit` defaults to 20. Graph search
defaults `include_evidence` to true. Omit that field entirely for `kind: "evidence"`. Relevance requires a query and rejects a cursor.

**Output:** bounded summaries and exact match references, `cursor`, `has_more`,
property filter mode/scan count, and live evidence index coverage. Relevance
returns the top 100 at most. Full properties/raw bytes require `kb_get` or
`kb_read_evidence`.

**Errors:** `INVALID` for kind-inapplicable fields, invalid cursor, query token/
byte limits, predicate AST (maximum 32 leaves and four group levels), or strict
timestamps; `LIMIT` when verification/response budgets expire; `BUSY` or storage
errors.

Property JSON Pointer traversal follows the actual object/array container.
`ne` does not match missing paths; use `exists: false`. Index uncertainty invokes
canonical JSON fallback and is reported. See [Evidence, search, and deletion](../guides/evidence-search.md).

## `kb_neighbors`

**Input:** 1–1,000 unique node UUIDs in `seed_ids`; `direction` defaults to
`both`; optional relation types; `depth` defaults to 1 (maximum 3);
`max_nodes`/`max_edges` default to 100/300 and cap at 1,000/3,000.

**Output:** ready nodes and stored directed relations, plus `truncated`, a reason
(`max_nodes`, `max_edges`, `deadline`, or `response_bytes`), and the unexpanded
frontier. It never infers relations.

**Errors:** `INVALID` for duplicate/bad/over-budget seeds; `NOT_FOUND` for missing
seeds; `BUSY` or storage errors. Relations hidden by pending deletion can make
traversal temporarily disconnected.
