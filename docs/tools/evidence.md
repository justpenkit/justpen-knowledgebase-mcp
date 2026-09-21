# Evidence tools

## `kb_ingest_evidence`

**Input:** exactly one non-null `path`, `text`, or strict `base64`; optional bare
lowercase `media_type`, `encoding` (`auto` default, `utf-8`, `utf-16le`,
`utf-16be`, `latin-1`), source label, and up to 100 graph targets. Inline decoded
bytes are capped at 256 KiB. Paths are server-workspace paths.

**Output:** a durable job result. Short work may return `status: "completed"`;
larger path work returns `status: "accepted"`. Fields include job/evidence IDs,
lane, state, progress, effective media type, index state/incomplete coverage,
warnings, attempts, and retention state. SHA-256 identity is calculated from the
exact stored bytes. Missing targets produce `TARGET_NOT_FOUND` warnings while
preserving the evidence.

Deduplication can complete with pending/incomplete coverage while another job
owns the text index. Repairing an unowned failed index emits
`INDEX_REPAIR_QUEUED`. `attempts` counts durable execution steps and claims,
including deferrals and takeover. Completed dedup results refresh the repair
warning from the current index owner; a failed repair no longer reports queued
work.

A verified ingest retains a separate durable blob locator before publishing
bytes. Canonical evidence admission clears that locator atomically; failure or
cancellation retains it for retry or retention cleanup. Retention preserves
bytes while any canonical record or other job owns them, including another
job awaiting purge. Corrupt progress flags its own job for attention without
blocking cleanup of unrelated blobs. Purge faults use a 30-second process-local
cooldown while other cleanup and ingest work can continue.

**Errors:** `INVALID` for source/base64/encoding/media/target rules;
`PATH_DENIED` for containment or unsafe files;
`CONFLICT` for incompatible metadata on existing bytes or `RECORD_DELETING`;
`IO_ERROR`, `BUSY`, `LIMIT`, or `INDEX_ERROR` for bounded runtime failure.

A `path` containing a NUL byte breaks a source rule and is rejected with
`INVALID`. Releases up to v0.2.0 reported `INTERNAL` for that spelling.

Missing media on text defaults to `text/plain`. Missing media on path/base64
defaults to `application/octet-stream`, emits
`MEDIA_TYPE_DEFAULTED_TEXT_INDEX_SKIPPED`, and stores raw-only evidence. A path
or inline body no larger than 256 KiB uses the short lane; a larger path uses the
bulk lane. Accepted work survives request disconnect.

Inline admission records size and SHA-256 before acceptance and checks both
before copying the staged input. Missing or mismatched fingerprints fail with
`IO_ERROR` and retain the input for diagnosis. Inline jobs accepted by earlier
unreleased prototypes without fingerprints must be submitted again; their
original bytes cannot be inferred safely.

## `kb_read_evidence`

**Input:** canonical `evidence_id`; `offset` defaults to 0; `length` defaults to
16,384 and caps at 65,536; `format` defaults to `text` and also accepts `base64`.

**Output:** evidence ID, SHA-256, total byte size, returned byte range, format,
and content. Managed filesystem paths are never returned.

**Errors:** `INVALID` when offset exceeds EOF or a text range splits an encoding
boundary; `NOT_FOUND` for absent evidence; `CONFLICT` with
`RECORD_DELETING` for pending deletion; `BUSY`, `LIMIT`, or I/O failure. Offset
equal to EOF succeeds with an empty range; base64 is the safe form for arbitrary
bytes.

For MIME eligibility, job retention, retries, and exact search behavior, see
[Evidence, search, and deletion](../guides/evidence-search.md).
