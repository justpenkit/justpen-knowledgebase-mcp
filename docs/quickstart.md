# Quickstart

## Prerequisites

Install [uv](https://docs.astral.sh/uv/) and create a workspace directory. uv
resolves Python and package artifacts in its own cache;
the running MCP writes its database, evidence, staging files, and locks only
under the configured workspace.

Clone and install the locked project, then start Python with `-B` before package
imports. This prevents cold-start bytecode writes beside installed modules:

```bash
git clone https://github.com/justpenkit/justpen-knowledgebase-mcp.git
cd justpen-knowledgebase-mcp
uv sync --locked
mkdir -p "$PWD/workspace"
JUSTPEN_KNOWLEDGEBASE_WORKSPACE_DIR="$PWD/workspace" \
  uv run python -B -m justpen_knowledgebase_mcp
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
        "-B",
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

## HTTP transport

Start the same runtime with an explicit transport:

```bash
JUSTPEN_KNOWLEDGEBASE_WORKSPACE_DIR="$PWD/workspace" \
  uv --directory /absolute/path/to/justpen-knowledgebase-mcp \
  run python -B -m justpen_knowledgebase_mcp \
  --transport http --host 127.0.0.1 --port 8934
```

Connect the MCP client to `http://127.0.0.1:8934/mcp`. The default loopback
listener still reports `authentication: "none"`. A non-loopback host also
requires `JUSTPEN_KNOWLEDGEBASE_ALLOW_NON_LOOPBACK=true`; that opt-in only
permits the bind. It does not add authentication or replace network access
controls, and it does not claim Origin validation. Put an appropriate
authenticated access layer in front of any externally reachable deployment.

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
