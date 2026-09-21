## Unreleased

### Feat

- replace the recon graph contract with catalog v2 node and relation types
- bundle a versioned ICANN Public Suffix List snapshot and Nmap-derived service-name registry
- add parent-scoped port, service, and finding identity with atomic scoped batch writes
- guard scoped children by rejecting scope-relation or parent deletion until the child is deleted
- add HTTP fingerprint and SSH host key pivots for cross-host clustering
- add cloud storage bucket, identity tenant, repository and exposed secret types
- add email, phone and domain-scoped MTA-STS policy types for the mail and registration surfaces
- accept scoped, cloud and source-control nodes as finding sources, CDN detection at the DNS layer,
    and endpoint-level CVE matches
- derive the parent-scope order, relation set and delete guard from the catalog manifest
- publish a catalog reference page generated from the manifest, with a node-to-relation matrix

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
