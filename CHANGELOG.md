## v0.4.0 (2026-09-23)

### BREAKING CHANGE

- service secure, previously stored as submitted for services that do not require it, must now be a boolean.
- updating the bundled PSL or service registry changes the catalog fingerprint; existing workspaces are then refused.
- the listed cve, finding, ip_address, domain, subdomain, certificate, presents_certificate, service and repository properties, previously stored as submitted, are now validated and reject null.
- finding requires rule (finding_rule) and matcher (finding_matcher_or_empty); its identity is (rule, matcher) under the parent and title is no longer identity; severity also accepts unknown.
- secret nodes and exposes_secret edges reject plaintext-bearing keys (value, secret, plaintext, password, token, key, credential, match, raw, rawv2, redacted, line) in any case and at any depth; secret kind, detector, verified and key_id, previously stored as submitted, are now validated.
- spf_record values longer than 4096 characters are now rejected.
- port number 0, asn value 0 and IPv4-mapped IPv6 addresses (in ip_address values and http_url hosts) are now rejected.
- an endpoint url whose query string fails http_url (a space, angle brackets, an invalid or lowercase percent escape, non-ASCII) is now rejected instead of stored without it.
- organization name and asn name, country and rir, and ip_cidr netname, country and rir, previously stored as submitted, are now validated and reject null.
- registered_through accepts only a whois_registration source (was domain); has_contact from domain or subdomain no longer accepts the roles registrant, admin, tech or billing, which attach to whois_registration.
- the cpe23_or_empty format is removed; technology.cpe must be a product-level cpe23 string, and runs_technology cpe (cpe23) and version (tech_version), previously stored as submitted, are now validated.
- endpoint status, title, content_length, content_type and webserver, certificate self_signed, cwe name, mta_sts_policy mode, max_age and mx, and federates_with namespace_type, previously stored as submitted, are now validated and reject null.
- kb_types formats values are objects {version, description} instead of strings.
- catalog v3; workspaces created with catalog v2 are refused at open and are not migrated.

### Feat

- **catalog**: bind the bundled registries into the fingerprint
- **catalog**: model credential principals and crawl links
- **catalog**: add cloud account and cloud resource types
- **catalog**: declare ASM attribute properties
- **catalog**: key findings on rule and matcher
- **catalog**: declare RDAP number-resource attributes
- **catalog**: move registrar and registrant contacts to registrations
- **catalog**: model domain registrations as scoped nodes
- **catalog**: validate technology CPE and per-host version
- **catalog**: validate the documented attribute conventions
- **catalog**: declare validated optional properties
- **catalog**: describe every type and format outside the fingerprint
- **catalog**: publish check and canonicalization ids
- **catalog**: bump the catalog contract to v3

### Fix

- **release**: keep forced terminal colour out of parsed output
- **catalog**: keep the wildcard answer flag off registrable domains
- **catalog**: accept hyphens in endpoint URL paths and queries
- **catalog**: declare the service secure flag
- **catalog**: accept underscore labels in endpoint hosts
- **catalog**: refuse secret plaintext in any case or depth
- **catalog**: bound spf_record values at 4096 characters
- **catalog**: reject placeholder and IPv4-mapped spellings
- **catalog**: validate an endpoint query before removing it

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
