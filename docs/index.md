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

- Node and relation types come from a fixed catalog returned by `kb_types`.
- Required identity properties are strictly validated; additional JSON
    properties remain available for scanner-specific facts.
- A natural identity maps to one node per workspace, so several parents can
    point to the same node without copying a parent UUID into its properties.
- Evidence IDs are `e_` plus the lowercase SHA-256 of the exact stored bytes.
- Writes and delete admission are atomic. Large native I/O continues as durable
    jobs with explicit pending, failure, retry, and retention state.
- Read and search responses expose pagination, truncation, index coverage, and
    canonical property fallback rather than silently omitting unknown results.

## Contributors

See [Getting started](contributing/getting-started.md), the
[pre-PR checklist](contributing/pr-checklist.md), and the short
[template update guide](contributing/template-updates.md).
