"""Bounded terminal metadata retention inside existing guarded transactions."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast
from uuid import UUID

from ..errors import ConflictError, NotFoundError, StorageIOError
from .evidence import stage_name
from .job_ownership import OTHER_BLOB_OWNER_SQL, row_metadata
from .jobs import TERMINAL, adjust_terminal_count, job_row, pending_owners, protected

if TYPE_CHECKING:
    import apsw

    from ..config import WorkspacePolicy


class RetentionBatch(TypedDict):
    """At most 100 candidates examined and a stable oldest-first continuation."""

    cursor: tuple[float, int]
    examined: int
    pruned: int
    marked: int
    invalid: int


CANDIDATES_SQL = """SELECT id,uuid,state,finished_at,error_code FROM (
 SELECT * FROM (SELECT id,uuid,state,finished_at,error_code FROM jobs WHERE state='completed' AND (finished_at,id)>(?,?) ORDER BY finished_at,id LIMIT 100)
 UNION ALL
 SELECT * FROM (SELECT id,uuid,state,finished_at,error_code FROM jobs WHERE state='failed' AND (finished_at,id)>(?,?) ORDER BY finished_at,id LIMIT 100)
 UNION ALL
 SELECT * FROM (SELECT id,uuid,state,finished_at,error_code FROM jobs WHERE state='cancelled' AND (finished_at,id)>(?,?) ORDER BY finished_at,id LIMIT 100)
) ORDER BY finished_at,id LIMIT 100"""

PROTECTED_COUNT_SQL = """WITH pending(job_id) AS (
 SELECT delete_job_id FROM nodes WHERE lifecycle='delete_pending'
 UNION SELECT delete_job_id FROM relations WHERE lifecycle='delete_pending'
 UNION SELECT delete_job_id FROM evidence WHERE lifecycle='delete_pending'
) SELECT count(*) FROM pending CROSS JOIN jobs ON jobs.uuid=pending.job_id
WHERE jobs.state IN ('completed','failed','cancelled')"""

CANDIDATE_ROWS_SQL = "SELECT * FROM jobs WHERE uuid IN (SELECT value FROM json_each(?))"


def _group(state: str) -> Literal["completed", "failed_cancelled"]:
    return "completed" if state == "completed" else "failed_cancelled"


def _unleased(row: dict[str, Any], now: float) -> bool:
    return row["state"] in TERMINAL and (row["lease_expires_at"] is None or row["lease_expires_at"] <= now)


def _eligible(connection: apsw.Connection, row: dict[str, Any], now: float) -> bool:
    return _unleased(row, now) and not protected(connection, row["uuid"])


def _candidate_rows(connection: apsw.Connection, job_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Read a whole candidate page once, never one durable row per candidate."""
    rows: dict[str, dict[str, Any]] = {}
    if not job_ids:
        return rows
    cursor = connection.execute(CANDIDATE_ROWS_SQL, (json.dumps(job_ids),))
    names: list[str] = []
    for value in cursor:
        names = names or [column[0] for column in cursor.get_description()]
        row = dict(zip(names, value, strict=True))
        rows[str(row["uuid"])] = row
    return rows


def _ownership(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Malformed metadata is diagnostic state, never deletion authority."""
    _canonical_uuid(row["uuid"])
    if row.get("error_code") == "JOB_METADATA_INVALID":
        raise StorageIOError("IO_ERROR: JOB_METADATA_INVALID")
    payload, progress, _result = row_metadata(row)
    return payload, progress


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise StorageIOError("IO_ERROR: malformed job ownership token")
    try:
        canonical = str(UUID(value))
    except ValueError as exc:
        raise StorageIOError("IO_ERROR: malformed job ownership token") from exc
    if canonical != value:
        raise StorageIOError("IO_ERROR: malformed job ownership token")
    return canonical


def _owned_token(job_id: str, token: object) -> str:
    return stage_name(_canonical_uuid(job_id), _canonical_uuid(token))


def _recorded_tokens(row: dict[str, Any]) -> list[str]:
    payload, progress = _ownership(row)
    tokens: list[str] = []
    if payload.get("input_stage") is not None and payload.get("input_token") is None:
        raise StorageIOError("IO_ERROR: recorded input ownership mismatch")
    if payload.get("input_token") is not None:
        token = payload["input_token"]
        if _owned_token(row["uuid"], token) != payload.get("input_stage"):
            raise StorageIOError("IO_ERROR: recorded input ownership mismatch")
        tokens.append(token)
    if progress.get("stage_token") is not None:
        token = progress["stage_token"]
        _owned_token(row["uuid"], token)
        if token not in tokens:
            tokens.append(token)
    return tokens


def recorded_blob(row: dict[str, Any]) -> str | None:
    """Validate the recorded digest before it enters any filesystem operation."""
    _ownership(row)
    return row.get("blob_sha256")


def _prune(connection: apsw.Connection, row: dict[str, Any]) -> None:
    connection.execute("DELETE FROM jobs WHERE uuid=?", (row["uuid"],))
    adjust_terminal_count(connection, row["state"], -1)
    connection.execute(
        "UPDATE settings SET retention=json_set(retention,'$.pruned_total',coalesce(json_extract(retention,'$.pruned_total'),0)+1) WHERE singleton=1"
    )


class JobRetention:
    """Selection, purge ownership and counter updates have one SQL authority."""

    @staticmethod
    def counts(connection: apsw.Connection) -> dict[str, int]:
        """Read maintained counters; ordinary terminal transitions never count the table."""
        return json.loads(connection.execute("SELECT terminal_job_counts FROM settings WHERE singleton=1").get)

    @staticmethod
    def reconcile(connection: apsw.Connection) -> None:
        """Explicit startup maintenance recovery reconciles counters through the state index."""
        counts = dict.fromkeys(("completed", "failed_cancelled"), 0)
        for state in TERMINAL:
            counts[_group(state)] += connection.execute("SELECT count(*) FROM jobs WHERE state=?", (state,)).get
        connection.execute("UPDATE settings SET terminal_job_counts=? WHERE singleton=1", (json.dumps(counts),))

    @staticmethod
    def batch(
        connection: apsw.Connection, policy: WorkspacePolicy, cursor: tuple[float, int], *, now: float | None = None
    ) -> RetentionBatch:
        """Skip protected owners while advancing at most 100 terminal rows per commit."""
        now = time.time() if now is None else now
        counts = JobRetention.counts(connection)
        pending = dict.fromkeys(counts, 0)
        for state, count in connection.execute("SELECT state,count(*) FROM jobs WHERE purge_pending=1 GROUP BY state"):
            pending[_group(state)] += count
        rows = list(connection.execute(CANDIDATES_SQL, cursor * 3))
        pruned = marked = invalid = 0
        examined = [job_id for _id, job_id, _state, _finished, code in rows if code != "JOB_METADATA_INVALID"]
        # Both probes read only tables this loop never writes, and `BEGIN IMMEDIATE`
        # excludes every other writer, so one page-wide probe returns exactly what a
        # probe per candidate returned. Candidate identifiers are unique across the
        # page because a job holds one state and the three arms are disjoint.
        durable = _candidate_rows(connection, examined)
        owners = pending_owners(connection, examined)
        for _identifier, job_id, state, finished, error_code in rows:
            if error_code == "JOB_METADATA_INVALID":
                invalid += 1
                continue
            group = _group(state)
            excess = counts[group] - pending[group] > getattr(policy, group + "_retention_count")
            expired = finished + getattr(policy, group + "_retention_seconds") <= now
            if not (excess or expired):
                continue
            row = durable.get(job_id)
            if row is None:
                raise NotFoundError("job not found")
            if row["purge_pending"] or not _unleased(row, now) or job_id in owners:
                continue
            try:
                tokens = _recorded_tokens(row)
                digest = recorded_blob(row)
            except StorageIOError:
                connection.execute("UPDATE jobs SET error_code='JOB_METADATA_INVALID' WHERE uuid=?", (job_id,))
                invalid += 1
                continue
            if tokens or digest is not None:
                connection.execute(
                    "UPDATE jobs SET purge_pending=1,purge_tokens=?,updated_at=? WHERE uuid=?",
                    (json.dumps(tokens), now, job_id),
                )
                pending[group] += 1
                marked += 1
            else:
                _prune(connection, row)
                counts[group] -= 1
                pruned += 1
        after = (float(rows[-1][3]), int(rows[-1][0])) if len(rows) == 100 else (0.0, 0)
        return {"cursor": after, "examined": len(rows), "pruned": pruned, "marked": marked, "invalid": invalid}

    @staticmethod
    def next_job(connection: apsw.Connection, after_id: int = 0) -> tuple[int, str] | None:
        """One durable purge candidate; failures do not starve later candidates."""
        value = connection.execute(
            "SELECT id,uuid FROM jobs WHERE purge_pending=1 AND id>? ORDER BY id LIMIT 1", (after_id,)
        ).fetchone()
        return None if value is None else (int(value[0]), str(value[1]))

    @staticmethod
    def next_file(connection: apsw.Connection, job_id: str) -> str | None:
        """Called under job bucket: recheck durable purge and canonical protection first."""
        row = job_row(connection, job_id)
        if not row["purge_pending"] or not _eligible(connection, row, time.time()):
            raise ConflictError("JOB_RETENTION_PROTECTED")
        try:
            tokens = json.loads(row["purge_tokens"])
        except (TypeError, ValueError) as exc:
            raise StorageIOError("IO_ERROR: malformed purge ownership metadata") from exc
        recorded = _recorded_tokens(row)
        if not isinstance(tokens, list):
            raise StorageIOError("IO_ERROR: malformed purge ownership metadata")
        values = cast("list[object]", tokens)
        validated = [token for token in values if isinstance(token, str) and token in recorded]
        if len(values) > 2 or len(validated) != len(values):
            raise StorageIOError("IO_ERROR: malformed purge ownership metadata")
        return validated[0] if validated else None

    @staticmethod
    def next_blob(connection: apsw.Connection, job_id: str) -> str | None:
        """Retain the locator until canonical ownership or orphan disposal is acknowledged."""
        row = job_row(connection, job_id)
        if not row["purge_pending"] or not _eligible(connection, row, time.time()):
            raise ConflictError("JOB_RETENTION_PROTECTED")
        _recorded_tokens(row)
        return recorded_blob(row)

    @staticmethod
    def blob_disposable(connection: apsw.Connection, job_id: str, digest: str) -> bool:
        """Under EX digest bucket, prove absence of canonical and outstanding peer ownership."""
        if JobRetention.next_blob(connection, job_id) != digest:
            raise ConflictError("PURGE_BLOB_CHANGED")
        if connection.execute("SELECT 1 FROM evidence WHERE sha256=?", (digest,)).get is not None:
            return False
        return connection.execute(OTHER_BLOB_OWNER_SQL, (digest, job_id)).get is None

    @staticmethod
    def acknowledge_blob(connection: apsw.Connection, job_id: str, digest: str) -> None:
        """Caller retains the digest bucket through unlink and this durable acknowledgement."""
        if JobRetention.next_blob(connection, job_id) != digest:
            raise ConflictError("PURGE_BLOB_CHANGED")
        connection.execute(
            "UPDATE jobs SET blob_sha256=NULL,progress=json_remove(progress,'$.verified_sha256') WHERE uuid=?",
            (job_id,),
        )

    @staticmethod
    def acknowledge(connection: apsw.Connection, job_id: str, token: str) -> None:
        """Only the durable head token can be acknowledged after a successful unlink."""
        if JobRetention.next_file(connection, job_id) != token:
            raise ConflictError("PURGE_TOKEN_CHANGED")
        connection.execute("UPDATE jobs SET purge_tokens=json_remove(purge_tokens,'$[0]') WHERE uuid=?", (job_id,))

    @staticmethod
    def finalize(connection: apsw.Connection, job_id: str) -> bool:
        """Prune once, only after all recorded files were durably acknowledged."""
        if connection.execute("SELECT 1 FROM jobs WHERE uuid=?", (job_id,)).get is None:
            return False
        if JobRetention.next_file(connection, job_id) is not None:
            return False
        if recorded_blob(job_row(connection, job_id)) is not None:
            return False
        _prune(connection, job_row(connection, job_id))
        return True

    @staticmethod
    def forget_clean_input(connection: apsw.Connection, job_id: str, token: str) -> None:
        """After normal orphan cleanup, drop only a ready blob's acknowledged input locator."""
        if connection.execute("SELECT 1 FROM jobs WHERE uuid=?", (job_id,)).get is None:
            return
        row = job_row(connection, job_id)
        payload, _progress = _ownership(row)
        _payload, _decoded, result = row_metadata(row)
        if not row["purge_pending"] and payload.get("input_token") == token and "evidence_id" in result:
            connection.execute(
                "UPDATE jobs SET payload=json_remove(payload,'$.input_token','$.input_stage','$.input_size','$.input_sha256') WHERE uuid=?",
                (job_id,),
            )

    @staticmethod
    def snapshot(connection: apsw.Connection) -> dict[str, Any]:
        """Maintenance read snapshot; public status serves the materialized cache only."""
        protected_count = connection.execute(PROTECTED_COUNT_SQL).get
        return {
            "terminal_counts": JobRetention.counts(connection),
            "protected_count": protected_count,
            "needs_attention": protected_count > 0,
            "pending_prune_count": connection.execute("SELECT count(*) FROM jobs WHERE purge_pending=1").get,
            "pruned_total": connection.execute(
                "SELECT coalesce(json_extract(retention,'$.pruned_total'),0) FROM settings WHERE singleton=1"
            ).get,
        }
