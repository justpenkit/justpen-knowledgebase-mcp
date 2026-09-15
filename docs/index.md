# justpen-knowledgebase-mcp

Knowledge base MCP for justpen pentesting framework

This MCP server communicates over stdio. After running `make setup`, start it
with:

```bash
uv run justpen-knowledgebase-mcp
```

The project starts at version `0.0.0`; follow the
[release process](contributing/release-process.md) when it is ready to release.

## Evidence workspace workflow

Keep scanner output inside the configured `WORKSPACE_DIR`, either by writing it
there directly or copying a completed output file there before ingestion. For
example, with `/srv/assessment` as the workspace:

```bash
cp ./capture.pcap /srv/assessment/capture.pcap
```

The evidence service accepts `{"path":"capture.pcap","media_type":"application/vnd.tcpdump.pcap"}`.
Paths are relative to the server workspace; ingestion copies bytes into managed,
content-addressed storage and leaves the source file intact. Do not edit the
source during ingestion. Managed database, evidence, staging, lock and log paths
are excluded from import. There is no additional import-directory allowlist.

With HTTP transport, these paths refer to the server's filesystem. A path on a
remote client's computer is not uploaded automatically; copy it to the server
workspace first, or supply a bounded inline text/base64 body. Inline input is
limited to 256 KiB of decoded bytes. Large local files become durable jobs that
continue after the requesting client disconnects.

The service currently stores and reads raw evidence and processes durable delete
jobs. MCP tool registration and text indexing are subsequent implementation
stages. Text candidates remain visibly pending and incomplete after raw storage;
they are not reported as fully indexed. See the [evidence contract](api.md#evidence-and-durable-jobs).

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
