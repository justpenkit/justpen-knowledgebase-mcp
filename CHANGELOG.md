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
