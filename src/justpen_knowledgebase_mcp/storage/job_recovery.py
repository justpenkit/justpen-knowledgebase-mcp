"""Bounded sparse owner-intent recovery; immutable authority survives job loss."""

from __future__ import annotations

import os
import time
from contextlib import ExitStack
from typing import TYPE_CHECKING, Any

from ..errors import StorageIOError
from .evidence import stage_name
from .job_ownership import row_metadata
from .jobs import INTENT_SQL, PENDING_SQL, JobStore, job_row, protected, rejection_payload

if TYPE_CHECKING:
    from collections.abc import Iterator

    import apsw

    from ..workspace import WorkspacePaths


# A rejection job owns the incident relations of a node it left ready and `rejected`. No other job
# can: a ready relation never touches a rejected node, so `kb_delete` never marks one.
REJECTED_ENDPOINT_SQL = (
    "SELECT n.uuid FROM relations r JOIN nodes n ON n.id IN (r.source_id,r.target_id) "
    "WHERE r.lifecycle='delete_pending' AND r.delete_job_id=? AND n.lifecycle='ready' AND n.ownership='rejected' "
    "ORDER BY r.id,n.id LIMIT 1"
)


def _lost_job_payload(connection: apsw.Connection, kind: str, job_id: str, *, cascade: bool) -> dict[str, Any]:
    """Rebuild a rejection job across its node and relation intents, else the one-kind delete."""
    rejected = connection.execute(REJECTED_ENDPOINT_SQL, (job_id,)).get
    if rejected is not None:
        return rejection_payload(rejected)
    owners = list(connection.execute(INTENT_SQL[kind], (job_id,)))
    return {"kind": kind, "ids": [owner[1] for owner in owners], "cascade": cascade}


def recover_intents(connection: apsw.Connection, kind: str, after_id: int) -> dict[str, int]:
    """Keyset at most 100 pending owners, retaining immutable accepted job IDs."""
    rows = list(connection.execute(PENDING_SQL[kind], (after_id,)))
    repaired = 0
    for row in rows:
        job_id = row[2]
        if connection.execute("SELECT 1 FROM jobs WHERE uuid=?", (job_id,)).get is None:
            payload = _lost_job_payload(connection, kind, job_id, cascade=bool(row[3]))
            JobStore.insert(connection, job_id, "delete", "short", payload)
            repaired += 1
    return {"after_id": rows[-1][0] if len(rows) == 100 else 0, "repaired": repaired}


def staging_disposable(connection: apsw.Connection, job_id: str, token: str) -> bool:
    """Caller holds admission job bucket; missing metadata is checked after that lock."""
    if connection.execute("SELECT 1 FROM jobs WHERE uuid=?", (job_id,)).get is None:
        return not protected(connection, job_id)
    row = job_row(connection, job_id)
    if row.get("error_code") == "JOB_METADATA_INVALID":
        return False
    try:
        payload, _progress, result = row_metadata(row)
    except StorageIOError:
        return False
    if token == payload.get("input_token"):
        return "evidence_id" in result
    return not (
        row["state"] == "running"
        and row["lease_token"] == token
        and row["lease_expires_at"] is not None
        and float(row["lease_expires_at"]) > time.time()
    )


class StageScan:
    """Incrementally enumerate at most 32 entries, never load the staging tree into RAM."""

    def __init__(self, workspace: WorkspacePaths) -> None:
        """Keep only a descriptor-backed directory iterator between bounded batches."""
        self.workspace = workspace
        self.iterator: Iterator[os.DirEntry[str]] | None = None
        self._stack = ExitStack()

    def batch(self) -> list[str]:
        """Return recognized job/token staging names; unknown files are never cleanup targets."""
        if self.iterator is None:
            self.iterator = self._stack.enter_context(os.scandir(self.workspace.managed_fd(self.workspace.tmp)))
        result: list[str] = []
        for _ in range(32):
            entry = next(self.iterator, None)
            if entry is None:
                self.close()
                break
            pieces = entry.name.split(".")
            if len(pieces) == 3 and pieces[2] == "stage":
                try:
                    if stage_name(pieces[0], pieces[1]) == entry.name:
                        result.append(entry.name)
                except ValueError:
                    continue
        return result

    def close(self) -> None:
        """Close iteration before workspace descriptors are released."""
        if self.iterator is not None:
            self._stack.close()
            self.iterator = None
