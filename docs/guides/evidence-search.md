# Evidence, search, and deletion

## Evidence identity and indexing

`kb_ingest_evidence` accepts exactly one of `path`, `text`, or strict `base64`.
It stores the original bytes without parsing, decompression, or extraction and
identifies them as `e_` plus their lowercase SHA-256 digest.

Inline `text` defaults to `text/plain`. Path and base64 default to
`application/octet-stream`, return
`MEDIA_TYPE_DEFAULTED_TEXT_INDEX_SKIPPED`, and remain raw-only with
`index_state: "not_applicable"`. Media type comes from the request, never a file
extension or sniffing. Text candidates are:

- every `text/*` type;
- `application/json`, `application/xml`, `application/javascript`,
    `application/x-ndjson`, `application/json-seq`, `application/yaml`,
    `application/x-yaml`, `application/toml`, and `message/http`;
- `application/*+json` and `application/*+xml`.

Supported encoding declarations are `auto`, `utf-8`, `utf-16le`, `utf-16be`,
and `latin-1`. `auto` recognizes UTF-8/UTF-16 BOMs and otherwise selects UTF-8;
UTF-32 and conflicting BOM declarations fail indexing without deleting raw
evidence.

Inline data and paths at most 256 KiB use the short lane and wait up to the
request deadline. Larger paths enter the bulk lane and normally return
`status: "accepted"`. Both paths produce durable jobs; a disconnect does not
cancel accepted work. Relative paths start at the pinned workspace root; root
aliases are accepted, but child symlinks are rejected. A copied job retry starts
again at byte 0.

## Search semantics

`kb_search` defaults to `query_mode: "literal"`, `sort: "id"`, `limit: 20`,
and, for graph records, `include_evidence: true`. Literal mode verifies the
requested token sequence and case against canonical text. Words mode requires
every distinct token, in any order. Both accept at most 32 query tokens and
2,048 UTF-8 bytes. Relevance sorting requires a query, returns only the top 100,
and has no cursor.

Tokenization is textual, not an IP parser. A literal `10.0.0.1` can match the
prefix in `10.0.0.1.5`; use a structured property predicate for an exact address.
Snippets are deliberately shortened to 512 UTF-8 bytes, while `matches` retains
exact byte/line references (up to 32) and reports independent truncation.

Property predicates use strict JSON scalar types and RFC 6901 paths:

```json
{
  "kind": "nodes",
  "properties": {
    "all": [
      {"path": "/status", "op": "in", "value": [200, 204]},
      {"path": "/scanner/confidence", "op": "gte", "value": 0.8},
      {"path": "/retired", "op": "exists", "value": false}
    ]
  }
}
```

Operators are `eq`, `ne`, `in`, `exists`, `gt`, `gte`, `lt`, and `lte`.
`in` accepts 1–100 strict scalar/null operands. `ne` is false for a missing path;
test absence explicitly with `exists: false`. Pointer components traverse objects
by key and arrays only by canonical decimal index (`/items/0`); numeric object
keys remain object keys. A pointer deeper than 16 levels can validly prove an
indexed canonical field is absent even though stored properties themselves are
limited to depth 16.

The property index prioritizes required paths, then paths without an array
ancestor, then array descendants. It holds at most 512 paths; path and
materialized string values each have a 1,024-byte acceleration budget. An
oversized string keeps a typed sentinel, and a long path may have no row, but the
full canonical JSON is never truncated. `paths_complete` means every path has a
row; `complete` additionally means every stored value is materialized. When the
index cannot decide a predicate, `canonical_fallback` and
`canonical_scan_count` show that the full JSON was checked. Unknown index state
never silently becomes a negative result.

Timestamp filters accept calendar-valid `YYYY-MM-DDTHH:MM:SS`, optional 1–6
fraction digits, and `Z` or a numeric offset; `-00:00` and leap seconds are
rejected. Server outputs use fixed six-digit UTC.

ID-order cursors bind the workspace, query epoch, kind, and filters. They are
live pagination positions rather than snapshots or authorization tokens. Pending
rows can be skipped between pages, and mutation/reindex/delete admission can
invalidate a cursor. Search coverage describes the current live workspace; a
completed full reindex job can separately report generation changes or busy
items it skipped.

## Raw reads and jobs

`kb_read_evidence` reads byte offsets, not character positions. Length defaults
to 16 KiB and is capped at 64 KiB. `base64` preserves any range; `text` rejects
boundaries that split an encoded character. An offset at EOF returns empty data,
while an offset beyond EOF is invalid.

Completed jobs are retained for up to 7 days or the newest 100,000; failed and
cancelled jobs together for up to 30 days or the newest 10,000. Either age or
count can prune normal terminal metadata sooner, after which `get` and `retry`
return `NOT_FOUND`. Failed delete jobs required by pending intent are protected
from normal pruning. `expires_at` is an age-policy boundary, not a promise that
the row lasts until that time.

## Durable deletion

`kb_delete` admits 1–100 unique IDs atomically. Any missing or already-pending ID
rejects the whole request. After delete intent commits, it cannot be cancelled;
the response may be inline `completed` or durable `accepted`. A failed job keeps
`needs_attention`, its blocker details, and a retry route through `kb_jobs`.

Calling delete again for a pending record returns `CONFLICT` with the owning
`job_id` and intent time. `DEPENDENCIES_EXIST` names bounded blockers and their
owner. With `cascade: false`, deleting a relation never deletes linked evidence;
it removes only the relation-to-evidence link as part of relation cleanup.

Every operation that tries to use pending evidence—raw read, source/link page,
new link, duplicate ingest publication, or reindex—uses the same
`RECORD_DELETING` meaning. After a disconnect, that error proves durable intent,
not that cleanup succeeded; inspect the owning job. `NOT_FOUND` only proves the
record is absent now and does not reconstruct whether an earlier call succeeded.
