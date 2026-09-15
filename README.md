# justpen-knowledgebase-mcp

Knowledge base MCP for justpen pentesting framework

## Development

Install uv, Git and Make, then run:

```bash
make setup
```

`make setup` installs the Python environment, development and documentation
tools, and Git hooks. Use `make install` to refresh dependencies without
installing hooks. No separate Node.js or npm setup is required.

Follow the [getting-started guide](docs/contributing/getting-started.md)
to finish repository setup, customize the starter and run the development gate.

The server uses Python 3.11–3.13 and communicates over stdio:

```bash
JUSTPEN_KNOWLEDGEBASE_WORKSPACE_DIR=/absolute/existing/workspace uv run python -B -m justpen_knowledgebase_mcp
```

SIGTERM and SIGINT request graceful cleanup with a 30-second waiting budget.
Native SQLite close/fsync may exceed this budget; `shutdown_timeout` is logged
to stderr and a host supervisor can stop an unresponsive process. Committed WAL
data is recovered on reopening. Unexpected server failures reach
the CLI and produce a nonzero exit status.

Claude Code and Codex share [AGENTS.md](AGENTS.md). Follow the
[agent setup guide](docs/contributing/agents.md) to activate
project instructions and the protected metadata approval gate.

## Documentation and releases

Run `make docs-serve` to preview the MkDocs site, including API reference pages
from the server docstrings. `make docs-build` checks internal links and anchors.

New projects start at `0.0.0`. Use the [release process](docs/contributing/release-process.md)
for Commitizen changelogs, annotated tags and automatic GitHub Releases.

## Template updates

This project records its source and version in `.copier-answers.yml`. Follow the
[template update guide](docs/guides/template-updates.md)
to preview updates on a branch, preserve local changes, and review conflicts.
