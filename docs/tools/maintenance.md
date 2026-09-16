# Maintenance tools

## `kb_status`

**Input:** none.

**Output:** one cached snapshot with `single_workspace`/`single_engagement`
scope, bind scope, `authentication: "none"`, database/WAL/retention/index
samples, process-local queues, and fixed capabilities. Samples explicitly report
availability, cache age, and staleness. Status is SQL-free at call time and
cannot measure external reader age.

WAL diagnostics include `maintenance_alive`, `maintenance_error`, and
`maintenance_failed_permanently`. A permanent local maintenance fault rejects
product operations with its nonretryable error category; status, control, and
coordinated shutdown remain available. Transient failures retry at a bounded
cadence and clear after a newer successful measurement. Physical-high assessment
is a process-local cached observation, so peers can briefly display different
phases; shared measurements and each process's admission checks still protect
the workspace independently.

A retired reader reduces local capacity while healthy owners continue. An
exhausted read lane or failed writer reports a stable storage error; ordinary
queue saturation remains retryable `BUSY`.

**Errors:** only bounded internal/configuration failure at the wrapper; stale or
unavailable sampled state normally remains successful structured data.

## `kb_delete`

**Input:** `kind`, 1–100 unique IDs, and `cascade` defaulting to false.

**Output:** durable delete job state with inline `completed` or `accepted`,
deleted IDs, attempts/progress, and blocker/attention metadata.

**Errors:** `NOT_FOUND` with all missing IDs rejects the whole batch;
`CONFLICT` for dependencies or repeated/pending delete, with bounded blocker,
owner job, and intent time; `BUSY`, `LIMIT`, or storage errors. Once intent
commits it cannot be cancelled. `cascade: true` removes incident relations and
links while preserving neighboring nodes and unselected evidence blobs.

## `kb_jobs`

**Input:** `action` defaults to `list`. List accepts optional state, `limit` 20,
and cursor. `get`, `cancel`, and `retry` require only `job_id` and reject list
fields.

**Output:** list returns jobs plus `next_cursor`; control/get returns one durable
job record. A retry resumes eligible failed/cancelled work from its supported
checkpoint; copied evidence restarts at byte 0.

Cancelling an already terminal non-delete job is idempotent and returns its
unchanged state. Delete intent and purge protection still reject cancellation.
`attempts` counts durable claims/steps, including normal multi-step work,
pressure deferrals and lease takeover; it is not a count of user retries.

Retention preserves canonical evidence, sources and links. A published blob
without a canonical owner is cleaned only under its digest lock after proving
that no nonpurging job can still use it. Uncertain ownership retains the job's
locator and reports attention; malformed ownership metadata can delay orphan
blob cleanup while unrelated metadata and staging cleanup continue.

**Errors:** `INVALID` for action/field combinations; `NOT_FOUND` after normal
retention pruning; `CONFLICT` for ineligible cancellation/retry, including
immutable pending delete intent and a row already being purged; `BUSY`, `LIMIT`,
or storage errors. Required failed delete jobs remain retention-protected.

## `kb_reindex`

**Input:** `kind` and exactly one of 1–100 unique `ids` or `all: true`. A
`media_type` or `encoding` correction is allowed only with one evidence ID.

**Output:** accepted/completed durable reindex job with state, progress,
`reused`, `coverage_incomplete`, `generation_changed_count`, `index_busy_count`,
and bounded sample IDs. A matching active full pass can be reused.

**Errors:** `INVALID` for ambiguous selection/override; `NOT_FOUND` for missing
IDs; `CONFLICT` for another incompatible full pass or pending deletion;
`INDEX_ERROR`, `BUSY`, `LIMIT`, or storage errors. Reindex rebuilds derived state
from current canonical records and never resets canonical data. Full-pass job
skip coverage is distinct from `kb_search.coverage`, which describes the live
workspace when that search runs.

Operational WAL retry and safe copy/upgrade procedures are in
[Workspace and operations](../guides/workspace.md).
