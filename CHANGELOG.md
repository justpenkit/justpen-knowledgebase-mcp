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
    output schemas are byte-identical. For the six paths validated from a tool
    signature the refusal happens one layer earlier, as `is_error` with no
    structured envelope, exactly as a wrong-length identifier is refused today;
    below the signatures it carries `INVALID`. Closing that last inconsistency
    needs the tool modules and is not done here.
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
- **graph**: return `LIMIT` instead of `INTERNAL` when the first item of an
    evidence `links` page exceeds the response budget. Such a page has no
    returned association kind to put in `next_cursor`, and encoding one raised
    an unmapped error. No reachable link item is large enough to reach this
    today, so no client behaviour changes.

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
    unchanged, with the same caveat.
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
- **status**: skip the `dbstat` page walk when the database is larger than
    262 144 pages. The walk visits every page at about 3.5 us per page, which
    cannot finish inside the sampler's one-second budget at that size. Above the
    threshold the derived page statistics are omitted from `kb_status` rather
    than delaying the sample.

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
    Workspaces open unchanged — the catalog data and its fingerprint are not
    touched — but an existing fork does not merge itself. A workspace holding
    both spellings keeps the upper-case edge under a key nothing will look up
    again; a workspace holding only the upper-case spelling gains a second edge
    on the first write after the upgrade. To repair one, find the `caa_issue`
    and `caa_issuewild` edges whose stored `parameters[].name` still contains an
    upper-case letter, `kb_delete` them, and write the fact once. Patching such
    an edge by `id` does not repair it: an id-patch keeps the existing row's
    identity key, leaving folded properties under a key that no longer matches
    them.

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
