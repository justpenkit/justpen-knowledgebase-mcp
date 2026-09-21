# justpen knowledge base MCP

`justpen-knowledgebase-mcp` gives MCP agents one durable, workspace-local place
to connect scanner evidence to a strict recon graph. The server computes graph
identity keys, stores evidence by SHA-256, and exposes incomplete indexing and
pending deletion states instead of treating them as success.

One server deployment owns one configured workspace and one engagement.
Requests cannot select another workspace, and `JUSTPEN_SESSION_ID` correlates
telemetry only; it never partitions graph data.

## Start here

- [Quickstart](quickstart.md) configures stdio or HTTP and ingests a first file.
- [Workspace guide](guides/workspace.md) explains paths, copying, recovery, and
    operational boundaries.
- [Graph guide](guides/graph.md) discovers the catalog and writes shared nodes.
- [Evidence and search](guides/evidence-search.md) covers raw bytes, indexing,
    exact filters, jobs, and deletion.
- [Tool reference](tools/index.md) documents all 11 MCP tools and their response
    semantics.
- [Telemetry](guides/telemetry.md) configures scoped OTLP export and correlation.

## Core guarantees

- Node and relation types come from a fixed catalog returned by `kb_types`, and
    tabulated in [Catalog types and formats](reference/catalog.md).
- Required identity properties are strictly validated; additional JSON
    properties remain available for scanner-specific facts.
- Catalog v2 models domains, subdomains, IP addresses and CIDRs, ASNs, DNS
    records, technologies, TLS cipher suites and fingerprints, HTTP fingerprints,
    SSH host keys, registrars, organizations, weaknesses, ports and services,
    findings, parameters, certificates, endpoints, CVEs, storage buckets,
    identity tenants, repositories, exposed secrets, MTA-STS policies, and
    email and phone contacts.
- Unscoped identity maps a natural identity to one node per workspace. Port,
    service, finding, DKIM record, parameter, and MTA-STS policy identity also
    includes the UUID of its single parent.
- New scoped children and their `has_open_port`, `has_service`, `has_finding`,
    `has_dkim_selector`, `has_parameter`, or `has_mta_sts_policy` relation commit
    atomically and cannot be re-parented.
- Evidence IDs are `e_` plus the lowercase SHA-256 of the exact stored bytes.
- Writes and delete admission are atomic. Large native I/O continues as durable
    jobs with explicit pending, failure, retry, and retention state.
- Read and search responses expose pagination, truncation, index coverage, and
    canonical property fallback rather than silently omitting unknown results.
- Catalog v1 workspaces fail closed at startup; catalog v2 requires a new
    workspace.

## Contributors

See [Getting started](contributing/getting-started.md), the
[pre-PR checklist](contributing/pr-checklist.md), and the short
[template update guide](contributing/template-updates.md).
