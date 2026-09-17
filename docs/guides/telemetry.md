# Telemetry

Telemetry is disabled by default. When enabled, the server exports MCP request
spans, fixed operational events, and low-cardinality request count/duration
metrics to an OTLP collector over HTTP/protobuf or gRPC.
Enabled telemetry requires FastMCP's `FASTMCP_TELEMETRY_MODE=native` (the default)
so the server can enrich its native request spans. A different FastMCP mode fails
startup configuration validation.

Complete the workspace and runtime setup in the [Quickstart](../quickstart.md)
first; the launch snippet below assumes its required workspace variable is set.

```bash
export JUSTPEN_KNOWLEDGEBASE_OTEL_ENABLED=true
export JUSTPEN_KNOWLEDGEBASE_OTEL_PROTOCOL=http/protobuf
export JUSTPEN_KNOWLEDGEBASE_OTEL_ENDPOINT=http://127.0.0.1:4318
export JUSTPEN_SESSION_ID=pentest-example
export JUSTPEN_KNOWLEDGEBASE_OTEL_RESOURCE_ATTRIBUTES=justpen.run.id=run-example,deployment.environment.name=local
uv run python -m justpen_knowledgebase_mcp
```

The endpoint above is the collector, separate from the MCP HTTP listener. Pass
the same variables through the client when it launches a stdio server.

## Session identity

`justpen.session.id` comes only from `JUSTPEN_SESSION_ID`. Enabled startup
requires a valid value even if every individual signal is disabled. It accepts
1–128 ASCII letters, digits, periods, underscores, colons, and hyphens.
Additional resource attributes cannot replace it. This ID correlates telemetry;
it does not partition the graph or choose a workspace.

## Settings

All settings except `JUSTPEN_SESSION_ID` use the
`JUSTPEN_KNOWLEDGEBASE_OTEL_` prefix. Booleans accept case-insensitive `true` or
`false`.

| Suffix                                              | Meaning / default                                                       |
| --------------------------------------------------- | ----------------------------------------------------------------------- |
| `ENABLED`                                           | Master switch; `false`.                                                 |
| `TRACES_ENABLED`, `LOGS_ENABLED`, `METRICS_ENABLED` | Per-signal switches; each defaults to `true`.                           |
| `SERVICE_NAME`                                      | Resource service name; `justpen-knowledgebase-mcp`.                     |
| `RESOURCE_ATTRIBUTES`                               | Percent-encoded comma-separated fields; reserved session ID is ignored. |
| `PROTOCOL`                                          | `http/protobuf` (default) or `grpc`.                                    |
| `ENDPOINT`, `HEADERS`, `TIMEOUT`                    | Collector base URL, headers, and positive timeout in seconds.           |
| `CERTIFICATE`, `CLIENT_CERTIFICATE`, `CLIENT_KEY`   | TLS file paths.                                                         |
| `COMPRESSION`, `INSECURE`                           | `none`/`gzip`/`deflate`; gRPC insecure-channel boolean.                 |
| `TRACES_*`, `LOGS_*`, `METRICS_*`                   | Per-signal protocol and exporter options.                               |
| `TRACES_SAMPLER`, `TRACES_SAMPLER_ARG`              | Sampler; default `parentbased_always_on`, ratio 0–1.                    |
| `BSP_*`, `BLRP_*`                                   | Positive queue, batch, schedule-delay, and export-timeout controls.     |
| `METRIC_EXPORT_INTERVAL`, `METRIC_EXPORT_TIMEOUT`   | Positive millisecond controls.                                          |
| `SHUTDOWN_TIMEOUT_MS`                               | Shared exporter cleanup budget; `5000`.                                 |

Per-signal options take precedence. Public names are the short scoped settings
above, such as `JUSTPEN_KNOWLEDGEBASE_OTEL_TRACES_ENDPOINT`; do not use scoped
names containing `EXPORTER_OTLP`. Ambient `OTEL_*` does not configure this
server: the CLI clears it before installing the validated scoped snapshot.

## Propagation and privacy

The server continues a valid incoming W3C `traceparent`/`tracestate` from HTTP or
MCP `_meta`; without valid context it creates a root span carrying the canonical
session resource. This is conditional continuation. It does not promise that
every live client or harness always propagates W3C context.

Codex and Claude tool-call metadata shapes are recognized without treating them
as graph identity. Ingest and reindex job steps create root spans linked to the
initiating request across restarts. Delete steps emit bounded telemetry without
persisting an initiating request link.

Exported request data uses fixed method, transport, outcome, known-tool, and
known-error categories. Metrics never label request, session, or job IDs.
Arguments, results, raw evidence, query text, credentials, exception messages,
collector responses, and arbitrary metadata are not exported. Dependency stderr
is sanitized, raw HTTP URL access logging is disabled, and ordinary diagnostics
remain on stderr. Bounded exporter queues may drop telemetry without failing MCP
calls.
