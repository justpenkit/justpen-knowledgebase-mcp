## Unreleased

### Fix

- **evidence**: make `disk_check_interval_bytes` a real free-space check
    interval. It previously only capped the copy read size, so at its 8 MiB
    default it could not change behaviour, and every 64 KiB read re-measured the
    device. Reads now use a fixed 64 KiB chunk and the free-space check runs
    once per configured interval of copied bytes. The field is published to
    clients through `policy` in `kb_status`, and it now actually controls
    something, so its meaning has changed: during an evidence copy, a device
    filling up is detected at the configured interval instead of every 64 KiB.
    A copy smaller than the interval is checked once, when the stage is created
    for the full expected size, rather than repeatedly mid-copy. At the 8 MiB
    default that means most copies are checked once. The check runs after the
    write that crosses the interval rather than before each write, so up to one
    interval of bytes can reach the device between two measurements. Lower the
    field to detect a filling device sooner, at the cost of one `fstatvfs` per
    interval.
- **evidence**: reject a NUL byte in an ingest `path` with `INVALID` instead of
    `INTERNAL`. `WorkspacePaths.relative` now refuses the spelling before
    traversal, and `EvidenceStore.source_stat` classifies any remaining
    `ValueError` as `IO_ERROR`. This changes the error code `kb_ingest_evidence`
    returns for such a path; no accepted path is newly rejected.
- **identity**: reject a non-canonical record identifier at every ingress path
    with `INVALID` instead of accepting a spelling that can never match a stored
    row. SQLite compares TEXT with BINARY collation, so an upper-case or
    brace-wrapped UUID silently reported an existing record as missing. The
    check now lives on the `RecordID` alias, which all eleven ingress paths
    share; previously only three of them reached `validate_record_id` and
    `TargetRef.id` enforced nothing but length. Response models carry a separate
    `StoredRecordID` alias with the old width-only rule, so a maximal
    `kb_neighbors` payload is not revalidated; the published tool input and
    output schemas are byte-identical. A refusal carries `INVALID` whichever
    layer catches it; see the `tools` entry below, which made the
    signature-validation layer answer with the same envelope.
- **stdio**: decode transport frames strictly. Invalid UTF-8 inside a JSON
    string value used to be replaced with U+FFFD, which left the document
    parseable, so corrupted text was accepted as data. Such a frame is now
    returned as a line that cannot parse as JSON, which the SDK already reports
    to the peer as a parse error without ending the session. At end of stream a
    truncated trailing frame now terminates one iteration later than before.
- **graph**: give every TXT version-tag spelling exactly one home. `txt_record`
    diverted on a bare version tag while the dedicated types require a delimited
    grammar, so ten spellings were refused by both and had nowhere to live —
    among them `v=spf1include:_spf.google.com ~all`, a missing space after the
    version tag and the most common real SPF misconfiguration. The diversion now
    asks the dedicated type's own value rule, read from the catalog at call
    time, so the two acceptances are complementary by construction and cannot
    drift apart. **This widens acceptance:** those spellings are stored as
    generic `txt_record`s from now on. Nothing accepted before is rejected, and
    every well-formed spelling still diverts to its dedicated type.
- **docs**: list `mta_sts_policy` and `has_mta_sts_policy` in both parent-scope
    enumerations on the graph tool page, and correct the page's account of TXT
    version-tag routing, which described the opposite of what the code did. Both
    enumerations are now pinned against `scope_relations()` by a test that
    derives the expected names from the catalog rather than restating them.
- **graph**: return `LIMIT` when the first item of an association page exceeds
    the response budget, for every view rather than only evidence `links`. An
    evidence `links` page has no returned association kind to put in
    `next_cursor`, and encoding one raised an unmapped `INTERNAL`; the other
    views returned `truncated: true` with a cursor that did not advance. No
    reachable association item is large enough to reach either route today, so
    no client behaviour changes.
- **storage**: report which part of the stored workspace contract differs. An
    incompatible workspace previously failed to open with
    `CONFIGURATION: maintenance unavailable`, identically for a wrong schema
    version, catalog version, catalog fingerprint, index format version or
    managed-path layout, because checkpoint maintenance is the first thing to
    touch the database at open and flattened every `ConfigurationError` into
    that one message. The compatibility guard now raises an authored reason
    naming the differing dimension, and maintenance propagates it the way it
    already propagated the unsupported-layout reasons. When several dimensions
    differ the coarsest is reported. The reason names the category only: it
    carries no stored value and no filesystem path, and a `ConfigurationError`
    from any other source is still replaced with `maintenance unavailable`. The
    message a client receives for a mismatch therefore changes; clients matching
    on the `CONFIGURATION` code are unaffected.
- **tools**: answer a call rejected by its published input schema with the
    `INVALID` application envelope instead of a bare error result. The envelope
    names each rejected argument path and the rule it broke, for example
    `INVALID: invalid tool request; seed_ids.0: non-canonical graph id: use the lowercase 8-4-4-4-12 spelling`. This makes one identifier refusal one shape:
    `kb_get.ids` previously answered an evidence ID under `kind=nodes` with an
    envelope and a non-canonical UUID without one, and nothing published let a
    client predict which. The rejection no longer repeats the value the client
    sent, at most three rules are reported, and an argument name longer than 64
    bytes is truncated. Clients that parsed the previous validator report text
    must read the envelope instead. A call that reaches no tool at all — an
    unknown tool name, or a frame the transport cannot parse — still fails
    without an envelope. The published input schemas are unchanged; `list_tools()`
    is byte-identical across this change.
- **docs**: state which stored-contract dimensions an incompatible workspace can
    name on open, that the coarsest is reported when several differ, and that a
    managed-paths reason means a different configured managed directory layout
    rather than a missing directory.

### Perf

- **traversal**: bound `kb_neighbors` with a running byte counter instead of
    re-serializing the whole accumulated response for every appended edge. The
    200 000-byte budget is unchanged. The accounting charges one separator byte
    per array member, so a page can stop at most one byte per array earlier than
    the whole-output check it replaces; measured against a real corpus and
    twelve near-boundary item sizes, no truncation boundary moved. Bytes
    serialized for one call fall from 8.9 MB to 59 KB at `max_edges=300` and
    from 98.6 MB to 196 KB at 1000; p50 latency falls from 106.4 ms to 6.3 ms
    and from 1263.7 ms to 23.1 ms.
- **search**: apply the same running byte counter to both `kb_search` pages and
    stop copying the accumulated item list once per candidate. The 245 000-byte
    budget is unchanged, with the same at-most-one-byte-per-array caveat. Bytes
    serialized for one page fall from 5.7 MB to 133 KB at `limit=100`; p50
    latency falls from 27.1 ms to 5.9 ms.
- **search**: retain the best relevance matches in a bounded heap instead of
    sorting the retained list once per candidate. Selection and ordering are
    unchanged. p50 falls from 159.1 ms to 140.3 ms at `limit=100`.
- **graph**: apply the same running byte counter to `kb_get` record pages
    (250 000 bytes) and association pages (245 000 bytes). Both budgets are
    unchanged. The record page carries the same caveat as traversal and search.
    The association page counts its items without the two enclosing bracket
    bytes the replaced check included, so it stops one byte later rather than
    earlier; against a 245 000-byte budget under a 262 121-byte bound neither
    direction is reachable.
- **maintenance**: reuse the validated WAL name between `allocation()` samples
    instead of revalidating the path on every call. The cost is dominated by
    path normalization, not by syscalls, and falls from 123.8 us to 4.1 us on
    the cached path; a minimal `kb_search` round trip is about 20% faster. No
    claim is made for writes, where the effect is not separable from noise.
    **Behaviour change:** a managed directory replaced under a running server,
    or a WAL that gains a hard link, can now go unnoticed for up to one second
    longer than before. After that the full check runs again and
    `check_product` degrades to `WAL_PRESSURE` until restart, as it does today.
    A name that stops validating is dropped from the cache rather than served
    until expiry, so the degraded state is reached once and stays.
- **jobs**: probe retention owners once per page instead of once per job. A full
    100-row retention page issued 398 read statements inside a single control
    transaction and now issues 7. The page still runs in that one transaction
    under `BEGIN IMMEDIATE`, so no retention decision moves.
- **status**: decline the `dbstat` page walk when `PRAGMA page_count` reports
    more than 262 144 pages. The walk visits every page at about 3.5 us per
    page, so the sampler's one-second budget buys roughly 289 000 pages; past
    that the walk already ran out of budget and published `last_error: "LIMIT"`
    with `cached_at: null`. Declining up front reaches the same published state
    without spending up to a full second of a reader thread every 300 seconds.
    Two workspaces lose a figure they used to get: one between the threshold and
    its own host's real break-even, and one whose bulk is canonical records with
    a small text index, since `page_count` counts the whole database rather than
    the indexed objects. Below the threshold nothing changes.
- **search**: publish `coverage` and `incomplete` from a maintained counter
    instead of counting the `evidence` table on every request. `coverage()` ran
    `SELECT index_state,incomplete,count(*) ... GROUP BY ...` once per
    `kb_search` and once per `kb_status` sample with no index to support it: a
    full scan of every `evidence` row plus a temporary B-tree, measured at
    5.4 ms over 10 000 rows, 136 ms over 200 000 and 563 ms over 800 000,
    growing linearly and visiting every row whether or not it is `ready`. At
    200 000 rows it was 99.9% of a minimal `kb_search` round trip and 84% of a
    large text search. The counts now live in a new `settings.evidence_coverage`
    column, maintained by triggers on `evidence` inside the writing transaction
    the way `search_fts` already is, and the read is a single row: 0.011 ms and
    two pages, flat from 10 000 to 800 000 rows. A minimal `kb_search` over an
    800 000-row corpus falls from 566 ms to 0.20 ms. The published fields are
    unchanged in name, shape and value, and the aggregate is recomputed from the
    rows at startup, beside the existing terminal-job reconcile.

### Refactor

- **graph**: delete the unreachable `_upsert` write path and the helpers only it
    used, `_endpoints`, `_deduplicate` and `_ref`. `kb_write` has always resolved
    mutations through preflight and persist. The dead path computed
    `identity_key` without `parent_id`, so every parent-scoped type would have
    collapsed across parents had it ever been re-wired. No behaviour changes.
- **query**: delete the duplicate in-process index-evidence implementation,
    `index_evidence`, `_leaf_evidence` and `_missing_evidence`. The live path is
    the compiled SQL plus the canonical `evaluate` fallback. No behaviour
    changes.

### Breaking

- **BREAKING:** fold CAA parameter names into one identity. The `name` of a
    `caa_issue` or `caa_issuewild` parameter is identity-bearing but was stored
    case-preserving, so `accounturi` and `accountURI` forked one fact into two
    edges. An ASCII name is now lower-cased during canonicalization, before
    validation and before the identity is derived, so the stored spelling and
    the identity it produces stay in agreement. A non-ASCII name is left alone,
    because folding it would admit a spelling the parameter rule rejects.
    The catalog data and its fingerprint are not touched, so this change alone
    would have left existing workspaces readable with an unmerged fork in them.
    The schema bump below overrides that: a workspace written before this
    release cannot be opened at all, so no forked pair survives into it and no
    repair procedure is needed. Were such a workspace reachable, the repair
    would be to delete the `caa_issue` and `caa_issuewild` edges whose stored
    `parameters[].name` still contains an upper-case letter and write each fact
    once; patching one by `id` would not repair it, because an id-patch keeps
    the existing row's identity key and would leave folded properties under a
    key that no longer matches them.

- **BREAKING:** the workspace schema is now version 3. The new
    `settings.evidence_coverage` column and its three `evidence` triggers change
    the stored layout, and `SchemaGuard.check` compares the contract for exact
    equality, so a workspace created by an earlier version is refused at open
    rather than upgraded. A workspace written by v0.2.0 reports `CONFIGURATION`
    with the schema-version reason described above, that being the first of the
    five contract dimensions to differ. There is no in-tree upgrade path: create
    a new workspace and re-ingest.

## v0.2.0 (2026-09-21)

### Feat

- **catalog**: add pivot, cloud, source-control and contact types
- **catalog**: add weakness and registry organization types
- **catalog**: add SVCB bindings, issuer edges and wildcard coverage
- **catalog**: add TLS fingerprint and registrar types
- **catalog**: drop the query string from a stored endpoint URL
- **catalog**: add scoped DKIM selector and endpoint parameter types
- **catalog**: add technology, DNS TXT family and TLS cipher types
- add scoped identity, atomic scoped writes, delete guards
- replace catalog with v2 recon node and relation contract
- add bundled PSL and service name registry loaders

### Fix

- **hooks**: ask about writes to protected files, not about reads
- skip taplo dependency on linux aarch64
- benchmark corpus redirect status and schema v2 test expectations

### Refactor

- **catalog**: derive the parent-scope order and delete guard

### Breaking

- **BREAKING:** reject catalog v1 workspaces at startup; create a new workspace for catalog v2

## v0.1.1 (2026-09-17)

### Fix

- harden job retention and startup diagnostics

## v0.1.0 (2026-09-17)

### Feat

- add session-correlated knowledge base telemetry
- expose recon tools and cached status
- search recon records and raw text evidence
- bound terminal job metadata retention
- add evidence storage and recoverable background jobs
- add typed recon filters and graph traversal
- add mutable recon nodes and relations
- coordinate workspace WAL maintenance and pressure
- add graph schema and bounded database lifecycle
- add workspace-contained SQLite runtime

### Fix

- handle null stdin and known startup failures
- isolate corrupt jobs before retry and claim
- enforce blob ownership and stop sampler loops
- preserve safe diagnostics and maintenance recovery
- isolate allocation scans from status counters
- track durable blob ownership outside job progress
- write socket stdio in cancellable bounded chunks
- cancel stdio relay tasks before channel teardown
- bound graph query work and report derived storage
- finish orphan cleanup before first blob publication
- preserve durable job ownership and fair cleanup
- preserve maintenance startup failure boundaries
- isolate runtime failures and preserve resource ownership
- reject wildcard IPv6 host aliases
- harden HTTP and telemetry request boundaries
- restore Python 3.11 typing and CI fixtures
- preserve durable work through WAL pressure
- flush buffered stdout before restoring MCP wire
- complete knowledge base transport lifecycle
- preserve index reservations and snippet coverage
- enforce evidence operation and response budgets
- reject empty search continuation cursors
- reject deeply nested cursors across Python versions
- preserve admission through failed SQLite cleanup
- observe every database shutdown trigger

### Perf

- intersect word search candidates by match unit
