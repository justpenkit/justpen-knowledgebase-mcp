# justpen-knowledgebase-mcp

A workspace-local MCP server for durable recon evidence, a typed mutable graph,
exact property filters, full-text search, and maintenance jobs.

## Install and run

Until the first release is tagged, install the locked project from a checkout:

```bash
git clone https://github.com/justpenkit/justpen-knowledgebase-mcp.git
cd justpen-knowledgebase-mcp
uv sync --locked
```

MCP clients should start Python with `-B` and provide an existing absolute
workspace. The [quickstart](docs/quickstart.md) has working checkout-based stdio
and HTTP configurations. uv keeps its interpreter and package cache outside the
workspace; the server keeps runtime files beneath the workspace.

The server exposes 11 MCP tools for catalog discovery, graph writes and reads,
evidence ingestion, search, traversal, deletion, jobs, reindexing, and status.
Start with the [graph workflow](docs/guides/graph.md),
[evidence and search workflow](docs/guides/evidence-search.md), and
[tool reference](docs/tools/index.md).

## Development

Install uv, Git, and Make, then run `make setup`. Use `make test-one TEST=tests/test_file.py::test_name` for focused feedback; the installed Git
hooks own the routine full gates. Contributor setup, PR, and release details
live under [docs/contributing](docs/contributing/getting-started.md).

Documentation is built strictly with `make docs-build`; its canonical
publication URL is
[justpen-knowledgebase-mcp.justpenkit.justmumu.com](https://justpen-knowledgebase-mcp.justpenkit.justmumu.com/).
