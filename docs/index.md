# justpen-knowledgebase-mcp

Knowledge base MCP for justpen pentesting framework

This MCP server communicates over stdio. After running `make setup`, start it
with:

```bash
uv run justpen-knowledgebase-mcp
```

Replace the example tools and this introduction with your server's behavior.
The project starts at version `0.0.0`; follow the
[release process](contributing/release-process.md) when it is ready to release.

## Contributor docs

- [Getting started](contributing/getting-started.md): install the tools with
    `make setup` and run the development gate.
- [Claude Code and Codex](contributing/agents.md): activate shared development
    rules and metadata permissions.
- [PR checklist](contributing/pr-checklist.md): branch, commits, tests, docs and
    merge style.
- [Lint & typing](contributing/lint-typing.md): Ruff and strict Pyright rules,
    including the suppression protocol.
- [API reference](api.md): configuration, errors, responses and example tools,
    rendered from their Python docstrings.
- [Template updates](guides/template-updates.md): bring future template changes
    into this project through Copier.
