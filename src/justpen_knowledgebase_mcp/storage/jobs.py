"""Short guarded job transactions; no filesystem operations or lock waits."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ..errors import ConflictError, InvalidParamsError, NotFoundError
from ..evidence import is_text_candidate
from ..models import JobResult
from .deletions import DeleteIntent, GraphDeletion
from .graph import row_by_id

if TYPE_CHECKING:
    import apsw

    from ..models import DeleteRequest

LEASE_SECONDS = 30
HEARTBEAT_SECONDS = 5
TERMINAL = frozenset(("completed", "failed", "cancelled"))
PENDING_SQL = {
    "nodes": "SELECT id,uuid,delete_job_id,delete_cascade,delete_requested_at FROM nodes WHERE lifecycle='delete_pending' AND id>? ORDER BY id LIMIT 100",
    "relations": "SELECT id,uuid,delete_job_id,delete_cascade,delete_requested_at FROM relations WHERE lifecycle='delete_pending' AND id>? ORDER BY id LIMIT 100",
    "evidence": "SELECT id,uuid,delete_job_id,delete_cascade,delete_requested_at FROM evidence WHERE lifecycle='delete_pending' AND id>? ORDER BY id LIMIT 100",
}
INTENT_SQL = {
    "nodes": "SELECT id,uuid,delete_job_id,delete_cascade,delete_requested_at FROM nodes WHERE lifecycle='delete_pending' AND delete_job_id=? ORDER BY id LIMIT 100",
    "relations": "SELECT id,uuid,delete_job_id,delete_cascade,delete_requested_at FROM relations WHERE lifecycle='delete_pending' AND delete_job_id=? ORDER BY id LIMIT 100",
    "evidence": "SELECT id,uuid,delete_job_id,delete_cascade,delete_requested_at FROM evidence WHERE lifecycle='delete_pending' AND delete_job_id=? ORDER BY id LIMIT 100",
}


@dataclass
class Claim:
    """One expiring capability; cached expiry never replaces the SQL commit fence."""

    job_id: str
    token: str
    kind: str
    lane: str
    payload: dict[str, Any]
    progress: dict[str, Any]
    expires_at: float
    cancelled: bool = False
    lost: bool = False
    result: dict[str, Any] = field(default_factory=dict[str, Any])

    def check(self) -> None:
        """Bound synchronous I/O by the last successfully renewed lease."""
        if self.lost or time.time() >= self.expires_at:
            raise ConflictError("CLAIM_LOST")
        if self.cancelled:
            raise ConflictError("JOB_CANCELLED")


def _row(connection: apsw.Connection, job_id: str) -> dict[str, Any]:
    cursor = connection.execute("SELECT * FROM jobs WHERE uuid=?", (job_id,))
    value = cursor.fetchone()
    if value is None:
        raise NotFoundError("job not found")
    names = [column[0] for column in cursor.get_description()]
    return dict(zip(names, value, strict=True))


def _counter(connection: apsw.Connection, state: str, delta: int) -> None:
    group = "completed" if state == "completed" else "failed_cancelled" if state in ("failed", "cancelled") else None
    if group is not None:
        path = f"$.{group}"
        connection.execute(
            "UPDATE settings SET terminal_job_counts=json_set(terminal_job_counts,?,json_extract(terminal_job_counts,?)+?) WHERE singleton=1",
            (path, path, delta),
        )


def _intent(kind: str, row: tuple[Any, ...]) -> DeleteIntent:
    return DeleteIntent(kind, row[0], row[1], row[2], bool(row[3]), row[4])


def protected(connection: apsw.Connection, job_id: str) -> bool:
    """Use sparse equality predicates, independent of ready graph cardinality."""
    return any(connection.execute(INTENT_SQL[kind], (job_id,)).fetchone() is not None for kind in INTENT_SQL)


class JobStore:
    """All methods run inside DatabaseWorkers' SchemaGuard transaction boundary."""

    @staticmethod
    def insert(connection: apsw.Connection, job_id: str, kind: str, lane: str, payload: dict[str, Any]) -> None:
        """Accept compact durable work; body bytes are forbidden at this boundary."""
        if {"text", "base64"} & payload.keys():
            raise InvalidParamsError("inline body cannot be stored in job")
        now = time.time()
        connection.execute(
            "INSERT INTO jobs(uuid,kind,lane,state,requested_at,updated_at,payload) VALUES(?,?,?,'queued',?,?,?)",
            (job_id, kind, lane, now, now, json.dumps(payload)),
        )

    @staticmethod
    def claim(connection: apsw.Connection, lane: str, kind: str, *, now: float | None = None) -> Claim | None:
        """Atomically claim queued/expired work using a fresh fencing capability."""
        now = time.time() if now is None else now
        value = connection.execute(
            "SELECT uuid FROM jobs WHERE lane=? AND kind=? AND purge_pending=0 AND coalesce(json_extract(progress,'$.awaiting_text_index'),0)=0 AND (state='queued' OR (state='running' AND lease_expires_at<=?)) ORDER BY id LIMIT 1",
            (lane, kind, now),
        ).get
        if value is None:
            return None
        token = str(uuid4())
        expires = now + LEASE_SECONDS
        connection.execute(
            "UPDATE jobs SET state='running',lease_token=?,lease_expires_at=?,updated_at=?,attempts=attempts+1 WHERE uuid=?",
            (token, expires, now, value),
        )
        row = _row(connection, value)
        return Claim(
            value,
            token,
            row["kind"],
            row["lane"],
            json.loads(row["payload"]),
            json.loads(row["progress"]),
            expires,
            bool(row["cancel_requested"]),
            result=json.loads(row["result"]),
        )

    @staticmethod
    def fence(connection: apsw.Connection, claim: Claim, *, now: float | None = None) -> dict[str, Any]:
        """Reject stale, expired, purging, and cancelled capabilities in the snapshot."""
        now = time.time() if now is None else now
        row = _row(connection, claim.job_id)
        if (
            row["state"] != "running"
            or row["lease_token"] != claim.token
            or float(row["lease_expires_at"]) <= now
            or row["purge_pending"]
        ):
            raise ConflictError("CLAIM_LOST")
        return row

    @staticmethod
    def heartbeat(connection: apsw.Connection, claim: Claim) -> tuple[float, bool]:
        """Renew through the bounded control lane, without taking a bucket lock."""
        row = JobStore.fence(connection, claim)
        expires = time.time() + LEASE_SECONDS
        connection.execute(
            "UPDATE jobs SET lease_expires_at=?,updated_at=? WHERE uuid=? AND lease_token=?",
            (expires, time.time(), claim.job_id, claim.token),
        )
        return expires, bool(row["cancel_requested"])

    @staticmethod
    def checkpoint(connection: apsw.Connection, claim: Claim, progress: dict[str, Any]) -> None:
        """Commit verified progress only under the live lease and cancellation flag."""
        row = JobStore.fence(connection, claim)
        if row["cancel_requested"]:
            raise ConflictError("JOB_CANCELLED")
        connection.execute(
            "UPDATE jobs SET progress=?,updated_at=? WHERE uuid=?", (json.dumps(progress), time.time(), claim.job_id)
        )

    @staticmethod
    def finish(
        connection: apsw.Connection, claim: Claim, state: str, result: dict[str, Any], *, now: float | None = None
    ) -> None:
        """Transition once and update retention counters in the same fenced commit."""
        if state not in TERMINAL:
            raise InvalidParamsError("invalid terminal state")
        JobStore.fence(connection, claim, now=now)
        current = time.time() if now is None else now
        connection.execute(
            "UPDATE jobs SET state=?,result=?,finished_at=?,updated_at=?,lease_token=NULL,lease_expires_at=NULL,error_code=? WHERE uuid=?",
            (state, json.dumps(result), current, current, result.get("error"), claim.job_id),
        )
        _counter(connection, state, 1)

    @staticmethod
    def release(connection: apsw.Connection, claim: Claim, progress: dict[str, Any]) -> None:
        """Commit bounded cleanup and checkpoint while yielding the lane/lease."""
        JobStore.fence(connection, claim)
        connection.execute(
            "UPDATE jobs SET state='queued',progress=?,updated_at=?,lease_token=NULL,lease_expires_at=NULL WHERE uuid=?",
            (json.dumps(progress), time.time(), claim.job_id),
        )

    @staticmethod
    def get(connection: apsw.Connection, job_id: str) -> dict[str, Any]:
        """Materialize bounded public metadata, excluding source/staging locators."""
        row = _row(connection, job_id)
        payload = json.loads(row["payload"])
        result = json.loads(row["result"])
        output = {
            "job_id": job_id,
            "kind": row["kind"],
            "state": row["state"],
            "lane": row["lane"],
            "attempts": row["attempts"],
            "progress": {
                key: value
                for key, value in json.loads(row["progress"]).items()
                if key in {"bytes", "chunks", "rows_deleted"}
            },
            "effective_media_type": result.get("effective_media_type", payload.get("media_type")),
            "index_state": result.get(
                "index_state", "pending" if is_text_candidate(payload.get("media_type", "")) else "not_applicable"
            ),
            "incomplete": result.get("incomplete", is_text_candidate(payload.get("media_type", ""))),
            "needs_attention": row["state"] == "failed" and protected(connection, job_id),
            "purge_pending": bool(row["purge_pending"]),
            "warnings": payload.get("warnings", []),
            **result,
        }

        return JobResult.model_validate(output).model_dump(mode="json", exclude_none=True)

    @staticmethod
    def cancel(connection: apsw.Connection, job_id: str) -> dict[str, Any]:
        """Never undo immutable delete intent, including between queued steps."""
        row = _row(connection, job_id)
        if row["kind"] == "delete":
            raise ConflictError("DELETE_ALREADY_COMMITTED")
        if row["purge_pending"]:
            raise ConflictError("JOB_PURGING")
        if row["state"] == "queued":
            connection.execute(
                "UPDATE jobs SET state='cancelled',cancel_requested=1,finished_at=?,updated_at=? WHERE uuid=?",
                (time.time(), time.time(), job_id),
            )
            _counter(connection, "cancelled", 1)
        elif row["state"] == "running":
            connection.execute("UPDATE jobs SET cancel_requested=1 WHERE uuid=?", (job_id,))
        return JobStore.get(connection, job_id)

    @staticmethod
    def retry(connection: apsw.Connection, job_id: str) -> dict[str, Any]:
        """Retain durable input/verified blob checkpoints and create a new attempt."""
        row = _row(connection, job_id)
        if row["purge_pending"]:
            raise ConflictError("JOB_PURGING")
        if row["state"] not in ("failed", "cancelled"):
            raise InvalidParamsError("only failed or cancelled jobs can retry")
        _counter(connection, row["state"], -1)
        connection.execute(
            "UPDATE jobs SET state='queued',result=json_remove(result,'$.error','$.reason','$.details'),cancel_requested=0,finished_at=NULL,lease_token=NULL,lease_expires_at=NULL,error_code=NULL,updated_at=? WHERE uuid=?",
            (time.time(), job_id),
        )
        return JobStore.get(connection, job_id)

    @staticmethod
    def admit_delete(connection: apsw.Connection, request: DeleteRequest, job_id: str) -> None:
        """Insert job and validate/mark every owner in a single caller transaction."""
        JobStore.insert(
            connection,
            job_id,
            "delete",
            "short",
            {"kind": request.kind, "ids": request.ids, "cascade": request.cascade},
        )
        GraphDeletion.prepare(connection, request, job_id)

    @staticmethod
    def delete_step(connection: apsw.Connection, claim: Claim) -> dict[str, Any] | None:
        """Bound all canonical cleanup and checkpoint/lease release in one commit."""
        JobStore.fence(connection, claim)
        kind = claim.payload["kind"]
        rows = list(connection.execute(INTENT_SQL[kind], (claim.job_id,)))
        if not rows:
            JobStore.finish(connection, claim, "completed", {"deleted_ids": claim.payload["ids"]})
            return None
        intent = _intent(kind, rows[0])
        step = GraphDeletion.step(connection, intent)
        progress = {"rows_deleted": claim.progress.get("rows_deleted", 0) + step.rows_deleted}
        if step.files_pending:
            owner = row_by_id(connection, "evidence", intent.owner_id)
            if owner is None:
                raise ConflictError("delete owner disappeared")
            progress.update(files_pending=intent.uuid)
            JobStore.release(connection, claim, progress)
            return {"evidence_id": intent.uuid, "sha256": owner["sha256"]}
        if len(rows) == 1 and step.done:
            JobStore.finish(connection, claim, "completed", {"deleted_ids": claim.payload["ids"], "progress": progress})
        else:
            JobStore.release(connection, claim, progress)
        return None

    @staticmethod
    def finalize_evidence(connection: apsw.Connection, claim: Claim, evidence_id: str) -> None:
        """After durable unlink, remove the retained owner under the same live intent."""
        JobStore.fence(connection, claim)
        row = row_by_id(connection, "evidence", evidence_id)
        if row is not None:
            if row["lifecycle"] != "delete_pending" or row["delete_job_id"] != claim.job_id:
                raise ConflictError("delete intent mismatch")
            connection.execute("DELETE FROM evidence WHERE id=?", (row["id"],))
        remaining = connection.execute(INTENT_SQL["evidence"], (claim.job_id,)).fetchone()
        if remaining is None:
            JobStore.finish(connection, claim, "completed", {"deleted_ids": claim.payload["ids"]})
        else:
            JobStore.release(connection, claim, {"rows_deleted": claim.progress.get("rows_deleted", 0) + 1})
