"""Canonical evidence admission and linkage in already guarded transactions."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from ..errors import ConflictError
from ..evidence import is_text_candidate
from ..identity import format_timestamp
from . import graph_sql
from .fulltext import index_owner_active
from .graph import require_ready, row_by_id
from .jobs import Claim, JobStore

if TYPE_CHECKING:
    import apsw


class EvidenceRecords:
    """Caller holds the evidence bucket before entering these short DB operations."""

    @staticmethod
    def publish_record(connection: apsw.Connection, claim: Claim, sha256: str, byte_size: int) -> dict[str, Any]:
        """Bind ready raw bytes, provenance and targets atomically after publication."""
        row = JobStore.fence(connection, claim)
        if row["cancel_requested"]:
            raise ConflictError("JOB_CANCELLED")
        existing = EvidenceRecords.check_existing(connection, claim, sha256)
        deduplicated = existing is not None
        options = claim.payload
        now = time.time_ns() // 1000
        identifier = "e_" + sha256
        if existing is None:
            media, encoding = options["media_type"], options["encoding"]
            state = "pending" if is_text_candidate(media) else "not_applicable"
            connection.execute(
                "INSERT INTO evidence(uuid,sha256,byte_size,media_type,encoding,blob_path,index_state,incomplete,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    sha256,
                    byte_size,
                    media,
                    encoding,
                    f"{sha256[:2]}/{sha256[2:4]}/{sha256}",
                    state,
                    int(state == "pending"),
                    now,
                    now,
                ),
            )
            existing = row_by_id(connection, "evidence", identifier)
        if existing is None:
            raise ConflictError("evidence disappeared")
        if options.get("source") is not None:
            stamp = format_timestamp(now)
            connection.execute(
                "INSERT INTO evidence_sources(evidence_id,source,first_seen_at,last_seen_at) VALUES(?,?,?,?) ON CONFLICT(evidence_id,source) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                (existing["id"], options["source"], stamp, stamp),
            )
        warnings = list(options["warnings"])
        active_index = index_owner_active(connection, existing["index_owner_job_id"])
        if existing["index_state"] == "index_failed" and not active_index:
            warnings.append("INDEX_REPAIR_QUEUED")
        for target in options["targets"]:
            owner = row_by_id(connection, target["kind"], target["id"])
            if owner is None:
                warnings.append("TARGET_NOT_FOUND")
                continue
            require_ready(connection, target["kind"], owner)
            connection.execute(graph_sql.LINK_ADD[target["kind"]], (owner["id"], existing["id"]))
        result = {
            "evidence_id": identifier,
            "effective_media_type": existing["media_type"],
            "index_state": existing["index_state"],
            "incomplete": bool(existing["incomplete"]),
            "warnings": warnings,
            "progress": {"bytes": byte_size, "chunks": 0},
        }
        connection.execute(
            "UPDATE jobs SET blob_sha256=NULL,progress=json_remove(progress,'$.stage_token','$.verified_sha256') WHERE uuid=?",
            (claim.job_id,),
        )
        if existing["index_state"] in ("pending", "index_failed") and not active_index:
            connection.execute("UPDATE jobs SET result=? WHERE uuid=?", (json.dumps(result), claim.job_id))
            JobStore.release(
                connection,
                claim,
                {
                    "bytes": byte_size,
                    "chunks": 0,
                    "awaiting_text_index": True,
                    "evidence_id": identifier,
                    "deduplicated": deduplicated,
                },
            )
        else:
            JobStore.finish(connection, claim, "completed", result)
        return result

    @staticmethod
    def finish_dedup(connection: apsw.Connection, claim: Claim, evidence_id: str) -> None:
        """A concurrent index owner does not undo already committed raw dedup/linkage."""
        job = JobStore.fence(connection, claim)
        if job["cancel_requested"]:
            raise ConflictError("JOB_CANCELLED")
        existing = row_by_id(connection, "evidence", evidence_id)
        if existing is None:
            raise ConflictError("evidence disappeared")
        require_ready(connection, "evidence", existing)
        warnings = [warning for warning in claim.result.get("warnings", []) if warning != "INDEX_REPAIR_QUEUED"]
        if existing["index_state"] in ("pending", "index_failed") and index_owner_active(
            connection, existing["index_owner_job_id"]
        ):
            warnings.append("INDEX_REPAIR_QUEUED")
        result = {
            **claim.result,
            "warnings": warnings,
            "effective_media_type": existing["media_type"],
            "index_state": existing["index_state"],
            "incomplete": bool(existing["incomplete"]),
        }
        JobStore.finish(connection, claim, "completed", result)

    @staticmethod
    def check_existing(connection: apsw.Connection, claim: Claim, sha256: str) -> dict[str, Any] | None:
        """Under bucket-before-DB order, reject pending or explicit metadata conflict."""
        JobStore.fence(connection, claim)
        existing = row_by_id(connection, "evidence", "e_" + sha256)
        if existing is not None:
            require_ready(connection, "evidence", existing)
            options = claim.payload
            if (options["media_explicit"] and options["media_type"] != existing["media_type"]) or (
                options["encoding_explicit"] and options["encoding"] != existing["encoding"]
            ):
                raise ConflictError("EVIDENCE_METADATA_CONFLICT")
        return existing
