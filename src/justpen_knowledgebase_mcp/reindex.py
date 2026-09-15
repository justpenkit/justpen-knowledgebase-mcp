"""Text indexing orchestration inside the existing durable JobRunner lanes."""

from __future__ import annotations

import contextlib
import json
import os
from typing import TYPE_CHECKING, Any, Self, TypeVar

from pydantic import Field, model_validator

from .errors import ConflictError, IndexingError, InvalidParamsError, McpError, NotFoundError
from .evidence import Encoding, is_text_candidate
from .identity import EvidenceID, validate_record_id
from .models import ClosedModel, Kind, MediaType, RecordID
from .storage.fulltext import IndexOwner, append_chunk, claim_item, clear_item_batch, finish_item, refresh_record_text
from .storage.graph import require_ready, row_by_id
from .storage.jobs import JobStore
from .storage.properties import refresh_properties
from .text import TextChunk, iter_chunks

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    import apsw

    from .jobs import JobRunner
    from .storage.jobs import Claim
    from .storage.worker import OperationToken


T = TypeVar("T")


def _live_job(connection: apsw.Connection, claim: Claim) -> dict[str, Any]:
    row = JobStore.fence(connection, claim)
    if row["cancel_requested"]:
        raise ConflictError("JOB_CANCELLED")
    return row


async def _guarded(runner: JobRunner, claim: Claim, callback: Callable[[apsw.Connection], T]) -> T:
    def commit(connection: apsw.Connection, _token: OperationToken) -> T:
        _live_job(connection, claim)
        return callback(connection)

    return await runner.workers.write(commit)


async def _index_stream(runner: JobRunner, claim: Claim, owner: IndexOwner) -> tuple[bool, int]:
    def open_chunks() -> Generator[TextChunk, None, None]:
        path = runner.store.workspace.evidence / runner.store.blob_name(owner.uuid[2:])
        with runner.store.workspace.open_managed_file(path) as fd, os.fdopen(os.dup(fd), "rb") as source:
            yield from iter_chunks(source, encoding=owner.encoding, check=claim.check)

    stream = await runner.io(claim.lane, open_chunks)
    incomplete, count = False, 0
    try:
        while True:
            chunk = await runner.io(claim.lane, lambda: next(stream, None))
            if chunk is None:
                break
            incomplete = incomplete or chunk.gap
            await _guarded(
                runner, claim, lambda c, chunk=chunk, count=count: _publish_chunk(c, claim, owner, chunk, count)
            )
            count += not chunk.gap
    finally:
        await runner.close_io_owner(claim.lane, stream.close)
    return incomplete, count


async def index_evidence(runner: JobRunner, claim: Claim, evidence_id: str) -> dict[str, Any]:
    """Rebuild immutable raw bytes under both job and item-generation fences."""
    owner = await _guarded(runner, claim, lambda c: claim_item(c, evidence_id, _live_job(c, claim)["id"], claim.token))
    incomplete, count = False, 0
    state = "ready" if is_text_candidate(owner.media_type) else "not_applicable"
    try:
        while not await _guarded(runner, claim, lambda c: clear_item_batch(c, owner)):
            claim.check()
        if state == "ready":
            incomplete, count = await _index_stream(runner, claim, owner)
        await _guarded(runner, claim, lambda c: finish_item(c, owner, state, incomplete=incomplete))
    except (McpError, OSError) as exc:
        failed_state = "index_failed" if isinstance(exc, (IndexingError, OSError)) else "pending"

        def failure(connection: apsw.Connection, _token: OperationToken) -> None:
            JobStore.fence(connection, claim)
            finish_item(connection, owner, failed_state, incomplete=True)

        # Replaced generation/claim owns its own coverage and release.
        with contextlib.suppress(ConflictError):
            await runner.workers.write(failure)
        raise
    return {
        "evidence_id": evidence_id,
        "effective_media_type": owner.media_type,
        "index_state": state,
        "incomplete": incomplete,
        "warnings": ["TOKEN_TOO_LONG"] if incomplete else [],
        "progress": {"bytes": owner.byte_size, "chunks": count},
    }


class ReindexRequest(ClosedModel):
    """Rebuild current canonical owners, with single-evidence metadata correction."""

    kind: Kind
    ids: list[RecordID | EvidenceID] | None = Field(default=None, min_length=1, max_length=100)
    all: bool = False
    media_type: MediaType | None = None
    encoding: Encoding | None = None

    @model_validator(mode="after")
    def selection(self) -> Self:
        """Reject ambiguous selections and bulk/graph metadata overrides."""
        if self.all == (self.ids is not None):
            raise ValueError("exactly one ids or all=true required")
        if self.ids is not None:
            if len(set(self.ids)) != len(self.ids):
                raise ValueError("duplicate ids")
            for identifier in self.ids:
                validate_record_id(self.kind, identifier)
        if self.model_fields_set & {"media_type", "encoding"}:
            if self.kind != "evidence" or self.ids is None or len(self.ids) != 1 or self.all:
                raise ValueError("overrides require one evidence id")
            if "media_type" in self.model_fields_set and self.media_type is None:
                raise ValueError("null media override")
            if "encoding" in self.model_fields_set and self.encoding is None:
                raise ValueError("null encoding override")
        return self


def admit_reindex(
    connection: apsw.Connection,
    request: ReindexRequest,
    job_id: str,
    *,
    initiating_context: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Claim the workspace full slot and metadata generations in BEGIN IMMEDIATE."""
    payload = request.model_dump(exclude_none=True)
    if request.all:
        active = connection.execute(
            "SELECT uuid,payload FROM jobs WHERE kind='reindex' AND json_extract(payload,'$.all')=1 AND state IN ('queued','running')"
        ).fetchone()
        if active is not None:
            active_payload = json.loads(active[1])
            active_payload.pop("_telemetry", None)
            if active_payload != payload:
                raise ConflictError("FULL_REINDEX_ACTIVE")
            return {**JobStore.get(connection, active[0]), "reused": True, "status": "accepted"}
    _validate_targets(connection, request)
    if initiating_context:
        payload["_telemetry"] = initiating_context
    JobStore.insert(connection, job_id, "reindex", "bulk", payload)
    if request.all:
        connection.execute("UPDATE settings SET query_epoch=query_epoch+1 WHERE singleton=1")
    if request.kind == "evidence" and request.model_fields_set & {"media_type", "encoding"}:
        identifier = (request.ids or [""])[0]
        row = row_by_id(connection, "evidence", identifier)
        if row is None:
            raise NotFoundError("evidence disappeared")
        media = request.media_type or row["media_type"]
        encoding = request.encoding or (row["encoding"] if is_text_candidate(media) else "auto")
        connection.execute(
            "UPDATE evidence SET media_type=?,encoding=?,index_generation=index_generation+1,index_owner_job_id=(SELECT id FROM jobs WHERE uuid=?),index_owner_token=?,index_state=?,incomplete=? WHERE id=?",
            (
                media,
                encoding,
                job_id,
                job_id,
                "pending" if is_text_candidate(media) else "not_applicable",
                int(is_text_candidate(media)),
                row["id"],
            ),
        )
    return {**JobStore.get(connection, job_id), "status": "accepted"}


def _next_record(connection: apsw.Connection, claim: Claim) -> tuple[int, str] | None:
    JobStore.fence(connection, claim)
    kind = claim.payload["kind"]
    after = claim.progress.get("after_id", 0)
    if claim.payload.get("all"):
        return connection.execute(NEXT_RECORD[kind], (after,)).fetchone()
    ids = claim.payload["ids"]
    index = claim.progress.get("item_index", 0)
    if index == len(ids):
        return None
    row = row_by_id(connection, kind, ids[index])
    if row is None:
        raise NotFoundError("reindex record not found")
    require_ready(connection, kind, row)
    return row["id"], row["uuid"]


async def reindex_step(runner: JobRunner, claim: Claim) -> None:
    """One canonical record/evidence per claim, yielding to other bulk jobs afterward."""
    selected = await runner.workers.read(lambda c, _t: _next_record(c, claim))
    if selected is None:
        await runner.workers.write(lambda c, _t: JobStore.finish(c, claim, "completed", claim.result))
        return
    identifier, uuid = selected
    result = dict(claim.result)
    if claim.payload["kind"] == "evidence":
        await _reindex_evidence_item(runner, claim, uuid, result)
    else:

        def refresh(connection: apsw.Connection, _token: OperationToken) -> None:
            job = JobStore.fence(connection, claim)
            if job["cancel_requested"]:
                raise ConflictError("JOB_CANCELLED")
            row = row_by_id(connection, claim.payload["kind"], uuid)
            if row is None:
                raise NotFoundError("record disappeared")
            require_ready(connection, claim.payload["kind"], row)
            refresh_properties(connection, claim.payload["kind"], row, json.loads(row["properties"]))
            refresh_record_text(connection, claim.payload["kind"], row)

        await runner.workers.write(refresh)

    def release(connection: apsw.Connection, _token: OperationToken) -> None:
        JobStore.fence(connection, claim)
        connection.execute("UPDATE jobs SET result=? WHERE uuid=?", (json.dumps(result), claim.job_id))
        JobStore.release(
            connection, claim, {"after_id": identifier, "item_index": claim.progress.get("item_index", 0) + 1}
        )

    await runner.workers.write(release)


_NEXT_TEMPLATE = "SELECT id,uuid FROM {table} WHERE id>? AND lifecycle='ready' ORDER BY id LIMIT 1"
NEXT_RECORD = {kind: _NEXT_TEMPLATE.format(table=kind) for kind in ("nodes", "relations", "evidence")}


def _validate_targets(connection: apsw.Connection, request: ReindexRequest) -> None:
    for identifier in request.ids or []:
        row = row_by_id(connection, request.kind, identifier)
        if row is None:
            raise NotFoundError("reindex record not found")
        require_ready(connection, request.kind, row)
        if request.kind == "evidence":
            media = request.media_type or row["media_type"]
            if request.encoding is not None and not is_text_candidate(media):
                raise InvalidParamsError("encoding requires text media")


async def _reindex_evidence_item(runner: JobRunner, claim: Claim, uuid: str, result: dict[str, Any]) -> None:
    try:
        indexed = await index_evidence(runner, claim, uuid)
        if not claim.payload.get("all") and len(claim.payload["ids"]) == 1:
            result.update(indexed)
        if indexed["incomplete"]:
            result["coverage_incomplete"] = True
    except (ConflictError, IndexingError) as exc:
        reason = str(exc)
        if not claim.payload.get("all") or (
            isinstance(exc, ConflictError)
            and reason not in ("INDEX_BUSY", "INDEX_GENERATION_CHANGED", "RECORD_DELETING")
        ):
            raise
        result["coverage_incomplete"] = True
        if reason in ("INDEX_BUSY", "INDEX_GENERATION_CHANGED"):
            field = "index_busy_count" if reason == "INDEX_BUSY" else "generation_changed_count"
            result[field] = result.get(field, 0) + 1
        sample = list(result.get("sample_ids", []))
        if len(sample) < 32:
            sample.append(uuid)
        else:
            result["sample_truncated"] = True
        result["sample_ids"] = sample


def _publish_chunk(connection: apsw.Connection, claim: Claim, owner: IndexOwner, chunk: TextChunk, count: int) -> None:
    append_chunk(connection, owner, chunk)
    JobStore.checkpoint(connection, claim, {**claim.progress, "chunks": count + int(not chunk.gap)})
