# Workspace and operations

## Path ownership

`JUSTPEN_KNOWLEDGEBASE_WORKSPACE_DIR` must be an existing absolute directory.
Relative evidence paths are resolved from its pinned root. An absolute import is
accepted only when it is lexically beneath either the configured root spelling
or its resolved root alias. Child symlinks, hard-linked source files, parent
components, managed database/evidence/staging paths, and paths outside the
workspace are rejected.

Import opens one stable regular source and copies its bytes into content-addressed
storage. A copy takeover or retry restarts at byte 0; partial copy progress is
not resumed. Do not modify the source while it is being copied.

## Capacity and concurrency

The default advisory reserve is 1.5 GiB: twice the 256 MiB WAL high-water mark
plus 1 GiB. The server checks the copy device first and separately checks other
managed devices, repeating during a copy every 8 MiB. This can reject early, but
it is not a hard quota or an atomic reservation. Database and evidence paths may
be on different devices, and concurrent/external writers can still consume space.

Database requests use one writer, 1–8 readers (2 by default), a shared reader
queue of 128, a writer queue of 128, and a control queue of 16. The 10-second
foreground deadline includes time waiting in those queues. Native evidence work
uses one short and one bulk lane with process-local bounded admission. Short
ingest and cleanup groups alternate when both are waiting; this is not global
round-robin or a completion-time promise.

## WAL pressure and retry

The persisted workspace policy, not a per-process environment override, controls
WAL and retention thresholds. At or above the low watermark for logical WAL
frames, while below pressure, maintenance tries a nonblocking opportunistic
reset. Under pressure it performs bounded drain/reset attempts. `kb_status.wal` exposes phase, `stale_normal`, cache ages, cooldown
fields, retry advice, and whether evaluation was requested. A cached normal
sample can be stale; a failed cache refresh is not a healthy zero.

For `BUSY`, `WAL_PRESSURE`, or reset-pending errors, honor `retry_after_ms` (at
least one second), add jitter, and increase subsequent delay within roughly
1–5 seconds. Re-check `kb_status`; do not assume a fixed 10–15 second completion
window.

An external `sqlite3` reader can hold back checkpointing. Keep read transactions
short and close the CLI promptly. `kb_status` cannot report the age of an
external reader it does not own.

## Upgrade and offline copy

Before an upgrade, stop every MCP process using the workspace. Reopen it with
the same compatible release. The startup guard checks schema/catalog/index
compatibility; it does not migrate an incompatible database automatically.

Unreleased v1 workspaces must also contain the required job scheduling and
ownership indexes with the expected definitions. A missing or incompatible
supporting index fails startup with `CONFIGURATION`; the service may report the
bounded message `maintenance unavailable`. Use the creating build to access or
export its data, preserve a complete offline copy, then recreate the workspace
or apply an explicitly reviewed offline upgrade. Startup does not install
missing indexes or modify an older layout silently.

For an offline backup or move:

1. Stop every MCP process and every other writer.
2. Copy `graph.sqlite3`, any `graph.sqlite3-wal` and `graph.sqlite3-shm`, the
    evidence directory, and every configured managed data/tmp/lock path as one
    coordinated set, including paths on different devices.
3. Preserve committed WAL after a hard stop. Never discard it or copy only
    `graph.sqlite3` and assume the result is current.

SIGTERM/SIGINT grants 30 seconds for coordinated shutdown, but that grace period
is not a hard timeout for native SQLite close or fsync. A supervisor may stop an
unresponsive process after diagnostics; remaining committed WAL is valid input
for SQLite recovery on the next open.

The v1 server logs diagnostics to stderr and optional OTLP exporters. It does not
create file logs and has no `LOG_DIR` setting.

For HTTP on a wildcard address, explicitly list the public Host aliases in
`JUSTPEN_KNOWLEDGEBASE_ALLOWED_HOSTS` as a JSON array, for example
`["kb.example.test"]`. Each entry is a concrete lowercase DNS name or IP literal;
up to 16 entries are accepted. This changes Host admission only. FastMCP still
checks Origin, and non-loopback binding still requires
`JUSTPEN_KNOWLEDGEBASE_ALLOW_NON_LOOPBACK=true`.
