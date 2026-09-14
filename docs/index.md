![justpen-mcp-dev-template logo](assets/logo.svg)

# justpen-mcp-dev-template

Start an MCP server with shared Claude Code and Codex rules, strict quality
checks and a repeatable path for template updates. Copier creates the project
from your answers and brings future template changes into your existing server.

## Build and maintain your MCP server

- [Create a project](guides/create-project.md): use GitHub's automatic setup or
    answer Copier's questions locally.
- [Claude Code and Codex](contributing/agents.md): activate shared instructions
    and the protected metadata gate.
- [Update your template](guides/template-updates.md): review incoming template
    changes and preserve project customizations.
- [Contribute](contributing/getting-started.md): set up the generator, run the
    quality gates and open a PR.

## One development setup

Install uv, then run `make setup`. It installs the Python environment, development
and documentation tools, and Git hooks. Formatting, tests, type checks and MkDocs
all run through Make; no separate Node.js or npm setup is required.

Projects support Python 3.11, 3.12 and 3.13, with 3.13 as the local default. The
generated documentation includes API reference pages built from the server's
Python docstrings. Read the [architecture guide](guides/template-architecture.md)
to see how the generator and generated project share their development policy.
