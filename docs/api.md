# API reference

These sections are generated from the server's Python docstrings. Update the
docstrings alongside public API changes, then run `make docs-build` to check the
reference and internal links.

## Configuration

::: justpen_knowledgebase_mcp.config

## Errors

::: justpen_knowledgebase_mcp.errors

## Response helpers

::: justpen_knowledgebase_mcp.responses

## Application lifecycle

::: justpen_knowledgebase_mcp.app

::: justpen_knowledgebase_mcp.service

The registry is currently empty while the real knowledgebase tools are implemented.
Successful tool results use `{"status":"ok","data":...}`; errors use
`{"status":"error","error":"CODE: message"}` with bounded operational details.

## Graph mutation contract

The service stores one node per validated catalog identity across the workspace.
Properties may contain additional JSON fields; those fields never change the
server-generated key. An ID patch retains omitted metadata, while explicit
`null` clears an optional label or source.

Object patches recursively merge. An empty object does not clear existing children:

```json
{"properties":{"scanner":{}}}
```

Given `{"scanner":{"status":"seen","note":"old"}}`, clear the object by
removing its child properties:

```json
{"remove_properties":["/scanner/status","/scanner/note"]}
```

The resulting property is `{"scanner":{}}`. Removing `/scanner/note` while
writing `/scanner/status` is valid. Removing `/scanner/status` while writing that
same path, or replacing `/scanner`, is invalid. Arrays are replaced as complete
values; removing an individual array element is unsupported. Required identity
fields cannot be removed or changed by a normal patch.

`observed_at` follows the last writer's payload, even when it describes an older
observation. It is not a maximum observation timestamp. Record reads preserve
whole properties and return `remaining_ids` when the response budget is reached.
Association cursors paginate live data without holding a snapshot or granting
additional access.

## Evidence and durable jobs

Evidence IDs are `e_` followed by the lowercase SHA-256 digest of the stored
bytes. Graph, job and workspace IDs remain UUIDs. The same bytes have one evidence
identity, and repeated imports retain every distinct non-null source in the
paginated `sources` view. Explicit conflicting media types or encodings fail
without changing the existing evidence metadata.

The service accepts exactly one of `path`, `text` or strict `base64`. It uses the
declared bare lowercase media type; filename extensions do not select indexing.
Text defaults to `text/plain`. Path/base64 default to `application/octet-stream`
and report `MEDIA_TYPE_DEFAULTED_TEXT_INDEX_SKIPPED`; declare the media type when
text indexing is intended. Text candidates include YAML, TOML and `message/http`.
Ingestion stores the original bytes without parsing, decompression or extraction.

Small inputs wait for storage up to the request deadline. Large paths return an
accepted job immediately. Accepted jobs survive request cancellation; job cancel
is a separate durable operation. Failed or cancelled pre-publication inline
jobs retain their owned input for retry. Deletes commit immutable pending intent
before cleanup, so their jobs cannot be cancelled after acceptance. Failed delete
jobs expose `needs_attention` and can be retried. Pending evidence remains visible
as metadata but rejects new reads, links and duplicate publication with
`RECORD_DELETING`.

Raw reads use byte offsets and return the hash, total size and exact returned
range. Length defaults to 16 KiB and is limited to 64 KiB. An offset at EOF returns
an empty range; an offset beyond EOF fails. Text ranges must end and begin on valid
encoding boundaries; use base64 for arbitrary byte ranges.

Each process runs one short and one bulk I/O worker, with bounded admission and
one active step per worker. Short ingest and cleanup take turns when both are
waiting. Durable jobs remain in SQLite; queue capacity is not a global job quota.
Text raw storage currently leaves the same accepted job queued with pending,
incomplete indexing. The next text-indexing stage continues that job rather than
claiming that raw storage completed text search support.

Before copying, the server checks available bytes on each managed device using
an advisory reserve of twice the WAL high-water threshold plus 1 GiB, and repeats
checks during copying. This is neither a quota nor an atomic reservation:
concurrent writers can still exhaust storage. An insufficient reserve or a failed
write is an I/O failure, not a successful import. Only owned incomplete staging
files are cleaned on failure; source files and published evidence are preserved.

Publication syncs regular files and affected directories and requests macOS
`F_FULLFSYNC` for evidence files. An unavailable or failed required sync fails
the operation. Process-kill recovery tests validate job/file reconciliation; they
do not establish power-loss durability for every filesystem or storage device.
