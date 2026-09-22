## v0.3.0 (2026-09-22)

### BREAKING CHANGE

- the workspace schema is now version 3. The new column and its
    triggers change the stored layout, so a workspace created by an earlier version
    is refused at open with `database contract or managed paths differ` instead of
    being upgraded. There is no in-tree upgrade path.
- `caa_issue` and `caa_issuewild` now store a lower-cased
    parameter `name`, and the identity of the edge follows the stored spelling.
    `accountURI` and `accounturi` described one fact and forked it into two edges;
    they are now one. This changes the server-generated identity of records already
    stored, so reverting the commit does not revert the records.

### Feat

- publish the identifier spelling clients must send
- fold CAA parameter names into one identity

### Fix

- measure the cached WAL through its pinned descriptor
- bound the dbstat walk by its deadline, not the file
- name which stored contract dimension differs
- answer a signature rejection with the public envelope
- give every TXT version-tag spelling exactly one home
- decode stdio transport lines strictly
- refuse a non-canonical record id at every ingress
- refuse an evidence link page that cannot encode a cursor
- **evidence**: make disk_check_interval_bytes a real check interval
- **evidence**: drop the duplicated disk reserve check in stage_inline
- **workspace**: open the evidence stage with O_CLOEXEC
- **evidence**: reject a NUL byte in a source path with INVALID
- normalize log_level after CLI override merge
- discard the stage when the blob is already canonical
- walk the whole hub relation identity product in the corpus

### Refactor

- delete the duplicate index-evidence implementation
- delete the unreachable graph upsert write path

### Perf

- maintain evidence coverage instead of counting it
- reuse the validated WAL name between allocation samples
- refuse a dbstat walk the sample budget cannot finish
- probe retention owners once per page, not per job
- rank search relevance with a bounded heap
- replace whole-output budget checks with byte counters
- batch reindex chunks into one guarded transaction
- share one parsed catalog for pure write-path reads
- release job waiters with a completion signal
- claim delete work through the active-lane index
- back idle job lanes off exponentially

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
