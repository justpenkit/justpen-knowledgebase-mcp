<img src="docs/assets/logo.svg" alt="justpen-knowledgebase-mcp logo" width="144" height="144">

# justpen-knowledgebase-mcp

A workspace-local MCP server that lets pentest agents share recon findings,
connect observations, and keep the raw evidence behind them. It runs on SQLite
and local files, without an LLM, embedding service, or separate database server.

## Features

- **Recon graph:** typed nodes and relations, server-generated identities,
    required property validation, and flexible additional properties.
- **Raw evidence:** store exact file, text, or base64 content and link it to
    graph records; read evidence back in bounded byte ranges.
- **Search and traversal:** full-text search across records and eligible text
    evidence, exact property filters, and bounded relation traversal.
- **Shared workspace:** concurrent agents can use the same knowledge base;
    durable jobs handle ingestion, deletion, and reindexing.
- **MCP transports:** stdio and Streamable HTTP, with 11 tools for the complete
    knowledge-base workflow.
- **Optional telemetry:** OpenTelemetry traces, logs, and metrics with incoming
    trace continuation and a shared `JUSTPEN_SESSION_ID`.

[Documentation](https://justpen-knowledgebase-mcp.justpenkit.justmumu.com/)
· [Tool reference](docs/tools/index.md)
· [Release notes](CHANGELOG.md)

## Install and run

Requires Python 3.11–3.13 on Linux or macOS. Install
[uv](https://docs.astral.sh/uv/), then run the pinned release with an existing
absolute workspace directory:

```bash
mkdir -p "$PWD/workspace"
JUSTPEN_KNOWLEDGEBASE_WORKSPACE_DIR="$PWD/workspace" \
  uv run --no-project --python 3.13 \
  --with "justpen-knowledgebase-mcp @ git+https://github.com/justpenkit/justpen-knowledgebase-mcp.git@v0.1.0" \
  python -m justpen_knowledgebase_mcp
```

This starts the stdio server. For HTTP, append
`--transport http --host 127.0.0.1 --port 8934` and connect to
`http://127.0.0.1:8934/mcp`. The HTTP listener has no built-in authentication;
keep it on loopback or supply an authenticated access layer.

Provide the workspace variable when configuring your MCP client.
The [quickstart](docs/quickstart.md) also has locked source-checkout installation
and client configuration examples.

The server exposes 11 MCP tools for catalog discovery, graph writes and reads,
evidence ingestion, search, traversal, deletion, jobs, reindexing, and status.
Call `kb_types` to discover the available schemas. Start with the [graph workflow](docs/guides/graph.md),
[evidence and search workflow](docs/guides/evidence-search.md), and
[tool reference](docs/tools/index.md). See [workspace operations](docs/guides/workspace.md)
for concurrency, backups, and upgrades, and [telemetry](docs/guides/telemetry.md)
for exporter configuration.

## Development

Install uv, Git, and Make, then run `make setup`. Use `make test-one TEST=tests/test_file.py::test_name` for focused feedback; the installed Git
hooks own the routine full gates. Contributor setup, PR, and release details
live under [docs/contributing](docs/contributing/getting-started.md).

Documentation is built strictly with `make docs-build`; its canonical
publication URL is
[justpen-knowledgebase-mcp.justpenkit.justmumu.com](https://justpen-knowledgebase-mcp.justpenkit.justmumu.com/).
