"""Bounded sparse owner-intent recovery; immutable authority survives job loss."""

from __future__ import annotations

import json
import os
import time
from contextlib import ExitStack
from typing import TYPE_CHECKING

from .evidence import stage_name
from .jobs import INTENT_SQL, PENDING_SQL, JobStore, protected

if TYPE_CHECKING:
    from collections.abc import Iterator

    import apsw

    from ..workspace import WorkspacePaths


def recover_intents(connection: apsw.Connection, kind: str, after_id: int) -> dict[str, int]:
    """Keyset at most 100 pending owners, retaining immutable accepted job IDs."""
    rows = list(connection.execute(PENDING_SQL[kind], (after_id,)))
    repaired = 0
    for row in rows:
        job_id = row[2]
        if connection.execute("SELECT 1 FROM jobs WHERE uuid=?", (job_id,)).get is None:
            owners = list(connection.execute(INTENT_SQL[kind], (job_id,)))
            JobStore.insert(
                connection,
                job_id,
                "delete",
                "short",
                {"kind": kind, "ids": [owner[1] for owner in owners], "cascade": bool(row[3])},
            )
            repaired += 1
    return {"after_id": rows[-1][0] if len(rows) == 100 else 0, "repaired": repaired}


def staging_disposable(connection: apsw.Connection, job_id: str, token: str) -> bool:
    """Caller holds admission job bucket; missing metadata is checked after that lock."""
    row = connection.execute(
        "SELECT payload,result,state,lease_token,lease_expires_at FROM jobs WHERE uuid=?", (job_id,)
    ).fetchone()
    if row is None:
        return not protected(connection, job_id)
    payload, result = json.loads(row[0]), json.loads(row[1])
    if token == payload.get("input_token"):
        return "evidence_id" in result
    return not (row[2] == "running" and row[3] == token and row[4] is not None and float(row[4]) > time.time())


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
