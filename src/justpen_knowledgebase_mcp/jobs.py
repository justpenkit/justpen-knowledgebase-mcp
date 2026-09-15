"""Durable job orchestration with independent bounded short and bulk I/O lanes."""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeVar
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from .cursors import CursorBinding
from .errors import (
    BusyError,
    ConflictError,
    InvalidParamsError,
    LimitError,
    McpError,
    NotFoundError,
    RecordConflictError,
    StorageIOError,
)
from .evidence import INLINE_LIMIT, IngestRequest, ReadEvidenceRequest
from .models import ClosedModel, DeleteRequest, RecordID
from .mutations import canonical_json
from .storage.evidence import EvidenceStore, StagedEvidence, job_bucket, stage_name
from .storage.evidence_records import EvidenceRecords
from .storage.graph import require_ready, row_by_id
from .storage.job_recovery import StageScan, recover_intents, staging_disposable
from .storage.jobs import HEARTBEAT_SECONDS, TERMINAL, Claim, JobStore
from .storage.worker import OperationToken

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Self

    import apsw

    from .config import WorkspacePolicy
    from .storage.worker import DatabaseWorkers
    from .workspace import WorkspacePaths

T = TypeVar("T")


class JobsRequest(ClosedModel):
    """Bounded lifecycle operations with one typed list cursor."""

    action: Literal["list", "get", "cancel", "retry"] = "list"
    job_id: RecordID | None = None
    state: Literal["queued", "running", "completed", "failed", "cancelled"] | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    cursor: str | None = None

    @model_validator(mode="after")
    def action_fields(self) -> Self:
        """Reject ambiguous control/list fields before database admission."""
        if self.action == "list":
            if self.job_id is not None:
                raise ValueError("list does not take job id")
        elif self.job_id is None or self.model_fields_set & {"state", "cursor", "limit"}:
            raise ValueError("control requires only job id")
        if self.job_id is not None:
            UUID(self.job_id)
        return self


class JobRunner:
    """Poll one durable step at a time; RAM queues are bounded independently of DB jobs."""

    def __init__(self, workers: DatabaseWorkers, workspace: WorkspacePaths, policy: WorkspacePolicy) -> None:
        """Allocate process-local lanes; persistence is exclusively in JobStore."""
        self.workers = workers
        self.store = EvidenceStore(workspace, policy)
        self._executors = {
            lane: ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"kb-io-{lane}") for lane in ("short", "bulk")
        }
        self._pending = {"short": 0, "bulk": 0}
        self._wake = {lane: asyncio.Queue[None](32) for lane in ("short", "bulk")}
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False
        self._stop_event = asyncio.Event()
        self._claims: dict[str, Claim] = {}
        self._recovery_after = dict.fromkeys(("nodes", "relations", "evidence"), 0)
        self.last_error: str | None = None
        self._stages = StageScan(workspace)
        self._orphans: deque[str] = deque()
        self._last_cleanup = "orphan"

    async def start(self) -> None:
        """Recover owner intent before beginning bounded durable polling."""
        await self.recover()
        self._tasks = [asyncio.create_task(self._lane(lane)) for lane in ("short", "bulk")]
        self._tasks.append(asyncio.create_task(self._recovery_loop()))

    async def close(self) -> None:
        """Drain I/O and heartbeat owners before DB worker/VFS teardown."""
        self._stopping = True
        self._stop_event.set()
        for claim in self._claims.values():
            claim.lost = True
        for lane in self._wake:
            self.wake(lane)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._stages.close()
        for executor in self._executors.values():
            await asyncio.to_thread(executor.shutdown, wait=True)

    def wake(self, lane: str) -> None:
        """Bound wake notifications; durable rows remain discoverable without a wake."""
        with contextlib.suppress(asyncio.QueueFull):
            self._wake[lane].put_nowait(None)

    async def io(self, lane: str, callback: Callable[[], T]) -> T:
        """At most one active operation and 32 admitted calls on each I/O lane."""
        if self._stopping or self._pending[lane] >= 32:
            raise BusyError("I/O lane admission unavailable")
        self._pending[lane] += 1
        future = asyncio.get_running_loop().run_in_executor(self._executors[lane], callback)
        try:
            return await asyncio.shield(future)
        finally:
            if future.done():
                self._pending[lane] -= 1
            else:
                future.add_done_callback(lambda _future: self._release_io(lane))

    def _release_io(self, lane: str) -> None:
        self._pending[lane] -= 1

    async def ingest(self, request: IngestRequest) -> dict[str, Any]:
        """Stage bounded input, accept durably, then await only the caller's deadline."""
        deadline = time.monotonic() + self.workers.factory.config.query_timeout_ms / 1000
        job_id, input_token = str(uuid4()), str(uuid4())
        options: dict[str, Any] = {
            "media_type": request.effective_media_type,
            "encoding": request.encoding,
            "media_explicit": request.media_type is not None,
            "encoding_explicit": "encoding" in request.model_fields_set,
            "source": request.source,
            "targets": [target.model_dump() for target in request.targets],
            "warnings": request.warnings,
        }
        if request.path is not None:
            source_stat = await self.io("short", lambda: self.store.source_stat(request.path or ""))
            options.update(path=str(self.store.workspace.relative(request.path)), source_stat=source_stat)
            lane = "short" if source_stat[2] <= INLINE_LIMIT else "bulk"
        else:
            await self._admit_inline(request, job_id, input_token, options, deadline)
            self.wake("short")
            return await self.wait(job_id, deadline)
        await self.workers.write(
            lambda c, _t: JobStore.insert(c, job_id, "ingest", lane, options), OperationToken(deadline)
        )
        self.wake(lane)
        return await self.wait(job_id, deadline) if lane == "short" else await self.accepted(job_id)

    async def _admit_inline(
        self, request: IngestRequest, job_id: str, token: str, options: dict[str, Any], deadline: float
    ) -> None:
        content = await self.io("short", request.inline_bytes)
        bucket = await self.acquire_bucket("short", job_bucket(job_id), exclusive=True)
        try:
            task = asyncio.create_task(self.io("short", lambda: self.store.stage_inline(content, job_id, token)))
            try:
                staged = await asyncio.shield(task)
            except asyncio.CancelledError:
                await _settle(task)
                await self.io("short", lambda: self.store.discard_stage(job_id, token))
                raise
            options.update(input_stage=staged.name, input_token=token, input_size=staged.byte_size)
            try:
                await self.workers.write(
                    lambda c, _t: JobStore.insert(c, job_id, "ingest", "short", options), OperationToken(deadline)
                )
            except BaseException:
                await self.io("short", lambda: self.store.discard_stage(job_id, token))
                raise
        finally:
            os.close(bucket)

    async def delete(self, request: DeleteRequest) -> dict[str, Any]:
        """Atomically accept immutable delete intent; request cancellation only stops waiting."""
        deadline = time.monotonic() + self.workers.factory.config.query_timeout_ms / 1000
        job_id = str(uuid4())
        await self.workers.write(lambda c, _t: JobStore.admit_delete(c, request, job_id), OperationToken(deadline))
        self.wake("short")
        return await self.wait(job_id, deadline)

    async def accepted(self, job_id: str) -> dict[str, Any]:
        """Return accurate current metadata for an already committed job acceptance."""
        result = await self.workers.read(lambda c, _t: JobStore.get(c, job_id))
        result["status"] = "accepted"
        return result

    async def wait(self, job_id: str, deadline: float) -> dict[str, Any]:
        """Cancellation leaves the accepted durable job alive; no task is tied to this waiter."""
        while time.monotonic() < deadline:
            result = await self.workers.read(lambda c, _t: JobStore.get(c, job_id))
            if result["state"] in TERMINAL:
                result["status"] = result["state"]
                return result
            await asyncio.sleep(min(0.01, max(0, deadline - time.monotonic())))
        return await self.accepted(job_id)

    async def control(self, request: JobsRequest) -> dict[str, Any]:
        """List or control jobs without exposing locators or copying evidence bodies."""
        if request.action == "list":
            return await self.workers.read(lambda c, _t: _list_jobs(c, request))
        if request.job_id is None:
            raise InvalidParamsError("job id required")
        if request.action == "get":
            return await self.workers.read(lambda c, _t: JobStore.get(c, request.job_id or ""))
        callback = JobStore.cancel if request.action == "cancel" else JobStore.retry
        result = await self.workers.control(lambda c, _t: callback(c, request.job_id or ""))
        self.wake(result["lane"])
        return result

    async def acquire_bucket(self, lane: str, digest: str, *, exclusive: bool) -> int:
        """Yield between NB attempts, so a waiting writer never occupies its reader's I/O lane."""
        deadline = time.monotonic() + self.workers.factory.config.query_timeout_ms / 1000
        while True:
            try:
                task = asyncio.create_task(
                    self.io(
                        lane, lambda: self.store.acquire_bucket(digest, exclusive=exclusive, deadline=time.monotonic())
                    )
                )
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    fd = await _settle(task)
                    os.close(fd)
                    raise
            except BusyError:
                if self._stopping or time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(min(0.005, deadline - time.monotonic()))

    async def read(self, request: ReadEvidenceRequest) -> dict[str, Any]:
        """Acquire SH bucket first, then recheck readiness in a guarded read snapshot."""
        digest = request.evidence_id[2:]
        fd = await self.acquire_bucket("short", digest, exclusive=False)
        try:

            def metadata(connection: apsw.Connection, _token: OperationToken) -> tuple[int, str]:
                row = row_by_id(connection, "evidence", request.evidence_id)
                if row is None:
                    raise NotFoundError("evidence not found")
                require_ready(connection, "evidence", row)
                return row["byte_size"], row["encoding"]

            size, encoding = await self.workers.read(metadata)
            return await self.io("short", lambda: self.store.read_slice(digest, size, encoding, request))
        finally:
            os.close(fd)

    async def recover(self) -> None:
        """Advance sparse owner cursors, so failed first batches cannot starve later owners."""
        for kind, after in self._recovery_after.items():
            result = await self.workers.control(lambda c, _t, kind=kind, after=after: recover_intents(c, kind, after))
            self._recovery_after[kind] = result["after_id"]
        if not self._orphans:
            self._orphans.extend(await self.io("short", self._stages.batch))

    async def _recovery_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=30)
            except TimeoutError:
                try:
                    await self.recover()
                except (BusyError, LimitError):
                    continue
                except (McpError, OSError):
                    self._failure_cache("IO_ERROR: periodic job recovery failed")

    async def _lane(self, lane: str) -> None:
        previous = "cleanup"
        while not self._stopping:
            classes = (
                ["ingest"]
                if lane == "bulk"
                else (["ingest", "cleanup"] if previous == "cleanup" else ["cleanup", "ingest"])
            )
            handled = False
            try:
                for category in classes:
                    if await self._category_step(lane, category):
                        handled = True
                        previous = category
                        break
            except (BusyError, LimitError):
                handled = False
            except (McpError, OSError):
                if not self._stopping:
                    self._failure_cache("IO_ERROR: background job admission failed")
            if not handled:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake[lane].get(), timeout=0.1)

    async def _category_step(self, lane: str, category: str) -> bool:
        if category == "cleanup" and self._orphans and self._last_cleanup == "delete":
            await self._orphan_step(self._orphans.popleft())
            self._last_cleanup = "orphan"
            return True
        kind = "delete" if category == "cleanup" else "ingest"
        claim = await self.workers.control(lambda c, _t: JobStore.claim(c, lane, kind))
        if claim is not None:
            await self._run_claim(claim)
            if category == "cleanup":
                self._last_cleanup = "delete"
            return True
        if category == "cleanup" and self._orphans:
            await self._orphan_step(self._orphans.popleft())
            self._last_cleanup = "orphan"
            return True
        return False

    async def _orphan_step(self, name: str) -> None:
        job_id, token, _suffix = name.split(".")
        bucket = await self.acquire_bucket("short", job_bucket(job_id), exclusive=True)
        try:
            disposable = await self.workers.control(lambda c, _t: staging_disposable(c, job_id, token))
            if disposable:
                await self.io("short", lambda: self.store.discard_stage(job_id, token))
        finally:
            os.close(bucket)

    async def _run_claim(self, claim: Claim) -> None:
        self._claims[claim.token] = claim
        heart = asyncio.create_task(self._heartbeat(claim))
        try:
            claim.check()
            if claim.kind == "ingest":
                await self._ingest_step(claim)
            else:
                await self._delete_step(claim)
        except (McpError, OSError) as exc:
            await self._fail_claim(claim, exc)
        finally:
            heart.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heart
            self._claims.pop(claim.token, None)

    async def _heartbeat(self, claim: Claim) -> None:
        while not self._stopping:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            backoff = 0.25
            while time.time() < claim.expires_at:
                try:
                    remaining = claim.expires_at - time.time()
                    token = OperationToken(time.monotonic() + min(remaining, 1))
                    claim.expires_at, claim.cancelled = await self.workers.control(
                        lambda c, _t: JobStore.heartbeat(c, claim), token
                    )
                    break
                except (BusyError, LimitError):
                    remaining = claim.expires_at - time.time()
                    await asyncio.sleep(max(0, min(remaining, backoff + secrets.randbelow(25) / 1000)))
                    backoff = min(1, backoff * 2)
                except McpError:
                    claim.lost = True
                    return
            else:
                claim.lost = True
                return

    async def _ingest_step(self, claim: Claim) -> None:
        options = claim.payload
        verified = None
        if "verified_sha256" in claim.progress:
            verified = await self.io(
                claim.lane,
                lambda: self.store.verify_blob(claim.progress["verified_sha256"], claim.progress["bytes"], claim.check),
            )
        staged = None
        if verified is not None:
            digest, byte_size = verified.sha256, verified.byte_size
        else:
            staged = await self._copy_input(claim)
            digest, byte_size = staged.sha256, staged.byte_size
            progress = {"bytes": byte_size, "chunks": 0, "verified_sha256": digest, "stage_token": claim.token}
            await self.workers.control(lambda c, _t: JobStore.checkpoint(c, claim, progress))
        bucket = await self.acquire_bucket(claim.lane, digest, exclusive=True)
        try:
            await self.workers.write(lambda c, _t: EvidenceRecords.check_existing(c, claim, digest))
            claim.check()
            if staged is not None:
                await self.io(claim.lane, lambda: self.store.publish(staged))
            elif verified is not None:
                await self.io(claim.lane, lambda: self.store.recheck_blob(verified))
            claim.check()
            await self.workers.write(lambda c, _t: EvidenceRecords.publish_record(c, claim, digest, byte_size))
        finally:
            os.close(bucket)
        if "input_token" in options and len(self._orphans) < 32:
            self._orphans.append(stage_name(claim.job_id, options["input_token"]))
            self.wake("short")

    async def _copy_input(self, claim: Claim) -> StagedEvidence:
        options = claim.payload
        if "path" in options:
            return await self.io(
                claim.lane,
                lambda: self.store.copy_path(
                    options["path"],
                    options["source_stat"],
                    claim.job_id,
                    claim.token,
                    claim.check,
                    short=claim.lane == "short",
                ),
            )

        def copy_input() -> StagedEvidence:
            if options["input_stage"] != stage_name(claim.job_id, options["input_token"]):
                raise StorageIOError("IO_ERROR: input ownership mismatch")
            with self.store.workspace.open_managed_file(self.store.workspace.tmp / options["input_stage"]) as fd:
                content = os.read(fd, INLINE_LIMIT + 1)
            claim.check()
            return self.store.stage_inline(content, claim.job_id, claim.token)

        return await self.io(claim.lane, copy_input)

    async def _delete_step(self, claim: Claim) -> None:
        pending = claim.progress.get("files_pending")
        if pending is None:
            await self.workers.write(lambda c, _t: JobStore.delete_step(c, claim))
            return
        digest = pending[2:]
        bucket = await self.acquire_bucket("short", digest, exclusive=True)
        try:

            def check(connection: apsw.Connection, _token: OperationToken) -> None:
                JobStore.fence(connection, claim)
                row = row_by_id(connection, "evidence", pending)
                if row is None or row["delete_job_id"] != claim.job_id or row["lifecycle"] != "delete_pending":
                    raise ConflictError("delete intent mismatch")

            await self.workers.control(check)
            claim.check()
            await self.io("short", lambda: self.store.unlink_blob(digest))
            await self.workers.write(lambda c, _t: JobStore.finalize_evidence(c, claim, pending))
        finally:
            os.close(bucket)

    async def _fail_claim(self, claim: Claim, error: BaseException) -> None:
        if claim.lost or self._stopping:
            return
        try:
            # Only this token's partial copy is disposable. Immutable admission input survives.
            await self.workers.control(lambda c, _t: JobStore.fence(c, claim))
            await self.io(claim.lane, lambda: self.store.discard_stage(claim.job_id, claim.token))
            state = "cancelled" if claim.cancelled or str(error) == "JOB_CANCELLED" else "failed"
            result: dict[str, Any] = {
                "error": error.error_type if isinstance(error, McpError) else "IO_ERROR",
                "reason": str(error) if isinstance(error, McpError) else "managed I/O failed",
            }
            if isinstance(error, RecordConflictError):
                result["details"] = error.details.model_dump(mode="json")
            await self.workers.control(lambda c, _t: JobStore.finish(c, claim, state, result))
        except (McpError, OSError):
            self._failure_cache("IO_ERROR: job failure could not be committed")

    def _failure_cache(self, message: str) -> None:
        if self.last_error != message:
            sys.stderr.write(message + "\n")
        self.last_error = message


def _list_jobs(connection: apsw.Connection, request: JobsRequest) -> dict[str, Any]:
    workspace, epoch = connection.execute("SELECT workspace_id,query_epoch FROM settings").get
    binding = CursorBinding(workspace, epoch, "jobs", None, "list", {"state": request.state})
    try:
        after = binding.decode(request.cursor) if request.cursor is not None else 0
    except ValueError as exc:
        raise InvalidParamsError("invalid job cursor") from exc
    rows = list(
        connection.execute(
            "SELECT id,uuid FROM jobs WHERE id>? AND (? IS NULL OR state=?) ORDER BY id LIMIT ?",
            (after, request.state, request.state, request.limit + 1),
        )
    )
    items: list[dict[str, Any]] = []
    size, last_id = 4096, after
    more = False
    for row in rows:
        item = JobStore.get(connection, row[1])
        size += len(canonical_json(item).encode("utf-8"))
        if len(items) == request.limit or size > 250000:
            more = True
            break
        items.append(item)
        last_id = row[0]
    return {"jobs": items, "next_cursor": binding.encode(last_id) if more else None}


async def _settle(task: asyncio.Task[T]) -> T:
    while not task.done():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(task)
    return task.result()
