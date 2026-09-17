# Quickstart

## Prerequisites

Install [uv](https://docs.astral.sh/uv/) and create a workspace directory for
the database, evidence, staging files, and locks.

Clone and install the locked project, then start the server:

```bash
git clone https://github.com/justpenkit/justpen-knowledgebase-mcp.git
cd justpen-knowledgebase-mcp
uv sync --locked
mkdir -p "$PWD/workspace"
JUSTPEN_KNOWLEDGEBASE_WORKSPACE_DIR="$PWD/workspace" \
  uv run python -m justpen_knowledgebase_mcp
```

## Stdio client configuration

Use absolute paths in the client configuration. This JSON shape works for MCP
clients that support `mcpServers`:

```json
{
  "mcpServers": {
    "justpen-knowledgebase": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/justpen-knowledgebase-mcp",
        "run",
        "python",
        "-m",
        "justpen_knowledgebase_mcp"
      ],
      "env": {
        "JUSTPEN_KNOWLEDGEBASE_WORKSPACE_DIR": "/absolute/path/to/justpen-knowledgebase-mcp/workspace"
      }
    }
  }
}
```

Call `kb_status` first. It reports `deployment_scope: "single_workspace"`,
`engagement_scope: "single_engagement"`, and
`request_workspace_selection: false`; every client connected to this process
shares that scope.

Stdio supports POSIX pipes and stream sockets with exclusive ownership of each
endpoint. Socket output uses cancellable sends of at most 64 KiB without changing
inherited file flags. Blocking macOS sockets wait for readiness before sending at
most `SO_SNDLOWAT` bytes (typically 2 KiB). Pipe writes remain bounded by
`PIPE_BUF` (512 bytes on macOS), so large responses require more writes. TTY
endpoints retain conservative one-byte writes; arbitrary terminal modes do not
have the same cancellation guarantee. Embedders that set a global socket timeout
also retain the one-byte descriptor path and must not race changes to that global
setting with transport writes.

Regular files, `/dev/null`, and terminals are also accepted; other character
devices are rejected at startup with a configuration error.

## HTTP transport

Start the same runtime with an explicit transport:

```bash
JUSTPEN_KNOWLEDGEBASE_WORKSPACE_DIR="$PWD/workspace" \
  uv --directory /absolute/path/to/justpen-knowledgebase-mcp \
  run python -m justpen_knowledgebase_mcp \
  --transport http --host 127.0.0.1 --port 8934
```

Connect the MCP client to `http://127.0.0.1:8934/mcp`. The default loopback
listener still reports `authentication: "none"`. A non-loopback host also
requires `JUSTPEN_KNOWLEDGEBASE_ALLOW_NON_LOOPBACK=true`. For a wildcard bind
with a public Host name, use:

```bash
JUSTPEN_KNOWLEDGEBASE_ALLOWED_HOSTS='["kb.example.test"]' \
JUSTPEN_KNOWLEDGEBASE_ALLOW_NON_LOOPBACK=true \
JUSTPEN_KNOWLEDGEBASE_WORKSPACE_DIR="$PWD/workspace" \
  uv run python -m justpen_knowledgebase_mcp --transport http --host 0.0.0.0
```

The host setting is a JSON array of at most 16 lowercase
DNS names or IP literals, without ports, schemes, paths, or wildcards. The bind
host and local defaults remain allowed; unknown Host names and invalid Origin
headers are rejected. Bind opt-in and Host validation do not add authentication
or replace network access controls. Put an appropriate authenticated access
layer in front of any externally reachable deployment.

With HTTP, `path` inputs name files on the server's workspace, never files on a
remote MCP client's machine. Inline `text` or strict `base64` is limited to
256 KiB of decoded bytes; there is no separate large-upload API.

## Ingest a scanner result

Prefer configuring scanners to write completed output directly beneath a
dedicated workspace subdirectory such as `recon/`. Otherwise, copy the finished
file byte-for-byte into the workspace before import:

```bash
mkdir -p "$PWD/workspace/recon"
cp ./nuclei.jsonl "$PWD/workspace/recon/nuclei.jsonl"
```

Then call `kb_ingest_evidence`:

```json
{
  "path": "recon/nuclei.jsonl",
  "media_type": "application/x-ndjson",
  "encoding": "utf-8",
  "source": "nuclei"
}
```

The result is either `status: "completed"` or `status: "accepted"` with a
durable `job_id`. For accepted work, inspect `kb_jobs` until it reaches a
terminal state. Use the returned `evidence_id` as provenance when writing the
structured graph; the [graph guide](guides/graph.md) shows that workflow.
