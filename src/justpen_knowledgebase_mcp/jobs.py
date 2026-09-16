"""Durable job orchestration with independent bounded short and bulk I/O lanes."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Annotated, Any, Generic, Literal, TypeVar
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
    WalBusyError,
)
from .evidence import INLINE_LIMIT, IngestRequest, ReadEvidenceRequest
from .models import ClosedModel, DeleteRequest, RecordID, RetentionPolicyView, RetentionStatus
from .mutations import canonical_json
from .reindex import index_evidence, reindex_step
from .storage.evidence import EvidenceStore, StagedEvidence, job_bucket, stage_name
from .storage.evidence_records import EvidenceRecords
from .storage.graph import require_ready, row_by_id
from .storage.job_ownership import row_progress
from .storage.job_recovery import StageScan, recover_intents, staging_disposable
from .storage.job_retention import JobRetention
from .storage.jobs import HEARTBEAT_SECONDS, TERMINAL, Claim, JobStore
from .storage.worker import OperationToken
from .telemetry.context import capture_job_context
from .telemetry.events import TelemetryEvents

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Self

    import apsw

    from .config import WorkspacePolicy
    from .storage.worker import DatabaseWorkers
    from .workspace import WorkspacePaths

T = TypeVar("T")


class _IOCall(Generic[T]):
    """Atomically abandon a queued callback without freeing its executor queue slot."""

    def __init__(self, callback: Callable[[], T], deadline: float | None) -> None:
        self.callback = callback
        self.deadline = deadline
        self.guard = threading.Lock()
        self.running = False
        self.abandoned = False

    def run(self) -> T:
        with self.guard:
            if self.abandoned:
                raise LimitError("I/O admission expired")
            if self.deadline is not None:
                OperationToken(self.deadline).check()
            self.running = True
        return self.callback()

    def abandon_queued(self) -> bool:
        with self.guard:
            self.abandoned = not self.running
            return self.abandoned


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
        self.events = TelemetryEvents(logger_provider=None, meter_provider=None)
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
        self._claim_after: dict[tuple[str, str], int] = {}
        self._recovery_after = dict.fromkeys(("nodes", "relations", "evidence"), 0)
        self.last_error: str | None = None
        self._stages = StageScan(workspace)
        self._orphans: deque[str] = deque()
        self._last_cleanup = "orphan"
        self._purge_after = 0
        self._purge_next_attempt = 0.0
        self._retention_event = asyncio.Event()
        self._retention_cursor = (0.0, 0)
        self._retention_due = 0.0
        self._retention_sample_due = 0.0
        self._retention_dirty = False
        self._retention_attention = False
        selected = policy.model_dump(include=set(RetentionPolicyView.model_fields))
        self._retention_cache = RetentionStatus(policy=RetentionPolicyView.model_validate(selected)).model_dump(
            mode="json"
        )

    async def start(self) -> None:
        """Recover owner intent before beginning bounded durable polling."""
        await self.recover()
        await self.workers.control(lambda c, _t: JobRetention.reconcile(c))
        with contextlib.suppress(BusyError, LimitError):
            await self.retention_pass(force=True)
        self._tasks = [asyncio.create_task(self._lane(lane)) for lane in ("short", "bulk")]
        self._tasks.append(asyncio.create_task(self._recovery_loop()))
        self._tasks.append(asyncio.create_task(self._retention_loop()))

    async def close(self) -> None:
        """Drain I/O and heartbeat owners before DB worker/VFS teardown."""
        self._stopping = True
        self._stop_event.set()
        self._retention_event.set()
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

    async def io(self, lane: str, callback: Callable[[], T], deadline: float | None = None) -> T:
        """Cancel expired queued work; drain active native owners before their caller unwinds.

        Active work returns its owned result even after expiry. Foreground callers
        check the same deadline before their next phase and release owned resources.
        """
        if deadline is not None:
            OperationToken(deadline).check()
        if self._stopping or self._pending[lane] >= 32:
            raise BusyError("I/O lane admission unavailable")

        call = _IOCall(callback, deadline)
        self._pending[lane] += 1
        future = asyncio.get_running_loop().run_in_executor(self._executors[lane], call.run)

        def consumed(done: asyncio.Future[T]) -> None:
            self._pending[lane] -= 1
            if not done.cancelled():
                done.exception()

        future.add_done_callback(consumed)
        try:
            async with asyncio.timeout_at(deadline):
                return await asyncio.shield(future)
        except TimeoutError as exc:
            if call.abandon_queued():
                raise LimitError("I/O admission deadline exceeded") from exc
            return await _settle(future)
        except asyncio.CancelledError:
            if not call.abandon_queued():
                with contextlib.suppress(Exception):
                    await _settle(future)
            raise

    async def close_io_owner(self, lane: str, callback: Callable[[], None]) -> None:
        """Close an existing native I/O owner on its lane even during shutdown."""
        future = asyncio.get_running_loop().run_in_executor(self._executors[lane], callback)
        await _settle(future)

    async def ingest(self, request: IngestRequest, deadline: float) -> dict[str, Any]:
        """Stage bounded input, accept durably, then await only the caller's deadline."""
        OperationToken(deadline).check()
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
            source_stat = await self.io("short", lambda: self.store.source_stat(request.path or ""), deadline)
            options.update(path=str(self.store.workspace.relative(request.path)), source_stat=source_stat)
            lane = "short" if source_stat[2] <= INLINE_LIMIT else "bulk"
        else:
            accepted = await self._admit_inline(request, job_id, input_token, options, deadline)
            self.wake("short")
            return await self.wait(job_id, deadline, accepted)
        accepted = await self._accept_ingest(job_id, lane, options, deadline)
        self.wake(lane)
        return await self.wait(job_id, deadline, accepted) if lane == "short" else accepted

    async def _admit_inline(
        self, request: IngestRequest, job_id: str, token: str, options: dict[str, Any], deadline: float
    ) -> dict[str, Any]:
        content = await self.io("short", request.inline_bytes, deadline)
        bucket = await self.acquire_bucket("short", job_bucket(job_id), exclusive=True, deadline=deadline)
        try:
            task = asyncio.create_task(
                self.io("short", lambda: self.store.stage_inline(content, job_id, token), deadline)
            )
            try:
                staged = await asyncio.shield(task)
            except asyncio.CancelledError:
                await _settle(task)
                await self.io("short", lambda: self.store.discard_stage(job_id, token))
                raise
            options.update(
                input_stage=staged.name, input_token=token, input_size=staged.byte_size, input_sha256=staged.sha256
            )
            try:
                return await self._accept_ingest(job_id, "short", options, deadline)
            except BaseException:
                await self.io("short", lambda: self.store.discard_stage(job_id, token))
                raise
        finally:
            os.close(bucket)

    async def _accept_ingest(self, job_id: str, lane: str, options: dict[str, Any], deadline: float) -> dict[str, Any]:
        initiating_context = capture_job_context()
        if initiating_context:
            options = {**options, "_telemetry": initiating_context}

        def accept(connection: apsw.Connection, _token: OperationToken) -> dict[str, Any]:
            JobStore.insert(connection, job_id, "ingest", lane, options)
            return {**JobStore.get(connection, job_id), "status": "accepted"}

        return await self.workers.write(accept, OperationToken(deadline))

    async def delete(self, request: DeleteRequest, deadline: float) -> dict[str, Any]:
        """Atomically accept immutable delete intent; retain its committed metadata."""
        job_id = str(uuid4())

        def accept(connection: apsw.Connection, _token: OperationToken) -> dict[str, Any]:
            JobStore.admit_delete(connection, request, job_id)
            return {**JobStore.get(connection, job_id), "status": "accepted"}

        accepted = await self.workers.write(accept, OperationToken(deadline))
        self.wake("short")
        return await self.wait(job_id, deadline, accepted)

    async def wait(self, job_id: str, deadline: float, accepted: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return the latest observed metadata without fresh admission after expiry."""
        while time.monotonic() < deadline:
            try:
                result = await self.workers.read(lambda c, _t: JobStore.get(c, job_id), OperationToken(deadline))
            except (BusyError, LimitError):
                if accepted is None:
                    raise
            else:
                if result["state"] in TERMINAL:
                    return {**result, "status": result["state"]}
                accepted = {**result, "status": "accepted"}
            await asyncio.sleep(min(0.01, max(0, deadline - time.monotonic())))
        if accepted is None:
            raise LimitError("job waiter deadline exceeded")
        return accepted

    async def control(self, request: JobsRequest, deadline: float) -> dict[str, Any]:
        """List or control jobs without exposing locators or copying evidence bodies."""
        if request.action == "list":
            return await self.workers.read(lambda c, _t: _list_jobs(c, request), OperationToken(deadline))
        if request.job_id is None:
            raise InvalidParamsError("job id required")
        if request.action == "get":
            return await self.workers.read(
                lambda c, _t: JobStore.get(c, request.job_id or ""), OperationToken(deadline)
            )
        callback = JobStore.cancel if request.action == "cancel" else JobStore.retry
        result = await self.workers.control(lambda c, _t: callback(c, request.job_id or ""), OperationToken(deadline))
        self.wake(result["lane"])
        self._retention_event.set()
        return result

    async def acquire_bucket(self, lane: str, digest: str, *, exclusive: bool, deadline: float | None = None) -> int:
        """Yield between NB attempts, so a waiting writer never occupies its reader's I/O lane."""
        if deadline is None:
            deadline = time.monotonic() + self.workers.factory.config.query_timeout_ms / 1000
        while True:
            try:
                task = asyncio.create_task(
                    self.io(
                        lane,
                        lambda: self.store.acquire_bucket(digest, exclusive=exclusive, deadline=time.monotonic()),
                        deadline,
                    )
                )
                try:
                    fd = await asyncio.shield(task)
                    try:
                        OperationToken(deadline).check()
                    except LimitError:
                        os.close(fd)
                        raise
                except asyncio.CancelledError:
                    fd = await _settle(task)
                    os.close(fd)
                    raise
                else:
                    return fd
            except BusyError:
                if self._stopping or time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(min(0.005, deadline - time.monotonic()))

    async def read(self, request: ReadEvidenceRequest, deadline: float) -> dict[str, Any]:
        """Acquire SH bucket first, then recheck readiness in a guarded read snapshot."""
        digest = request.evidence_id[2:]
        fd = await self.acquire_bucket("short", digest, exclusive=False, deadline=deadline)
        try:

            def metadata(connection: apsw.Connection, _token: OperationToken) -> tuple[int, str]:
                row = row_by_id(connection, "evidence", request.evidence_id)
                if row is None:
                    raise NotFoundError("evidence not found")
                require_ready(connection, "evidence", row)
                return row["byte_size"], row["encoding"]

            size, encoding = await self.workers.read(metadata, OperationToken(deadline))
            result = await self.io("short", lambda: self.store.read_slice(digest, size, encoding, request), deadline)
            OperationToken(deadline).check()
            return result
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
                (["ingest", "reindex"] if previous != "ingest" else ["reindex", "ingest"])
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
        if category == "cleanup":
            return await self._cleanup_step()
        key = lane, category
        claim, self._claim_after[key] = await self.workers.control(
            lambda c, _t: JobStore.claim_batch(c, lane, category, self._claim_after.get(key, 0))
        )
        if claim is None:
            return False
        await self._run_claim(claim)
        return True

    async def _cleanup_step(self) -> bool:
        classes = ["delete", "orphan", "purge"]
        start = (classes.index(self._last_cleanup) + 1) % len(classes)
        for category in classes[start:] + classes[:start]:
            if await self._cleanup_category(category):
                self._last_cleanup = category
                return True
        return False

    async def _cleanup_category(self, category: str) -> bool:
        if category == "delete":
            claim = await self.workers.control(lambda c, _t: JobStore.claim(c, "short", "delete"))
            if claim is not None:
                await self._run_claim(claim)
                return True
        elif category == "orphan":
            if self._orphans:
                name = self._orphans.popleft()
                try:
                    await self._orphan_step(name)
                except (BusyError, LimitError):
                    if len(self._orphans) < 32:
                        self._orphans.append(name)
                    raise
                return True
        else:
            if time.monotonic() < self._purge_next_attempt:
                return False
            try:
                item = await self.workers.control(lambda c, _t: JobRetention.next_job(c, self._purge_after))
                self._purge_after = 0 if item is None else item[0]
                if item is not None:
                    await self._purge_step(item[1])
                    return True
            except (ConflictError, StorageIOError, OSError):
                self._purge_next_attempt = time.monotonic() + 30
                self._retention_attention = True
                self._retention_cache["needs_attention"] = True
                self._failure_cache("IO_ERROR: job retention purge needs attention")
                return True
        return False

    async def _purge_step(self, job_id: str) -> None:
        digest = None
        bucket = await self.acquire_bucket("short", job_bucket(job_id), exclusive=True)
        try:
            token = await self.workers.control(lambda c, _t: JobRetention.next_file(c, job_id))
            if token is not None:
                await self.io("short", lambda: self.store.discard_stage(job_id, token))

            def finish(connection: apsw.Connection, _token: OperationToken) -> str | None:
                if token is not None:
                    JobRetention.acknowledge(connection, job_id, token)
                if JobRetention.finalize(connection, job_id) or token is not None:
                    return None
                return JobRetention.next_blob(connection, job_id)

            digest = await self.workers.control(finish)
            self._retention_dirty = True
            self._retention_event.set()
        except NotFoundError:
            pass  # Another process already finalized this candidate before the bucket recheck.
        finally:
            os.close(bucket)
        if digest is not None:
            await self._purge_blob_step(job_id, digest)

    async def _purge_blob_step(self, job_id: str, digest: str) -> None:
        """One proven orphan unlink, outside the staging/job bucket scope."""
        bucket = await self.acquire_bucket("short", digest, exclusive=True)
        try:
            disposable = await self.workers.control(lambda c, _t: JobRetention.blob_disposable(c, job_id, digest))
            if disposable:
                await self.io("short", lambda: self.store.unlink_orphan_blob(digest))

            def acknowledge(connection: apsw.Connection, _token: OperationToken) -> None:
                JobRetention.acknowledge_blob(connection, job_id, digest)
                JobRetention.finalize(connection, job_id)

            await self.workers.control(acknowledge)
            self._retention_dirty = True
            self._retention_event.set()
        except NotFoundError:
            pass
        except ConflictError:
            self._retention_attention = True
            self._retention_cache["needs_attention"] = True
            raise
        finally:
            os.close(bucket)

    def queue_status(self) -> dict[str, Any]:
        """Snapshot event-loop-owned I/O admission counts without waiting."""
        return {
            "short": {"pending": self._pending["short"], "capacity": 32},
            "bulk": {"pending": self._pending["bulk"], "capacity": 32},
            "stopping": self._stopping,
        }

    def retention_status(self) -> dict[str, Any]:
        """Serve a bounded copy without DB operations or file locks, including cache age."""
        snapshot = dict(self._retention_cache)
        stamp = snapshot["cached_at"]
        snapshot["cache_age"] = None if stamp is None else max(0, time.time() - stamp)
        snapshot["stale"] = snapshot["stale"] or stamp is None or time.time() - stamp >= 60
        return RetentionStatus.model_validate(snapshot).model_dump(mode="json")

    async def retention_pass(self, *, force: bool = False) -> bool:
        """One control batch on age/count trigger, followed by a cached read snapshot."""
        try:
            counts = await self.workers.read(lambda c, _t: JobRetention.counts(c))
            policy = self.store.policy
            due = force or self._retention_cursor != (0.0, 0) or time.time() >= self._retention_due
            excess = (
                counts["completed"] > policy.completed_retention_count
                or counts["failed_cancelled"] > policy.failed_cancelled_retention_count
            )
            changed = False
            if due or excess:
                if self._retention_cursor == (0.0, 0):
                    self._retention_attention = False
                batch = await self.workers.control(lambda c, _t: JobRetention.batch(c, policy, self._retention_cursor))
                self._retention_cursor = batch["cursor"]
                changed = bool(batch["pruned"] or batch["marked"])
                if batch["invalid"]:
                    self._retention_attention = True
                    self._failure_cache("IO_ERROR: retention ownership metadata needs attention")
                if self._retention_cursor == (0.0, 0):
                    self._retention_due = time.time() + 3600
                if batch["marked"]:
                    self.wake("short")
            if force or changed or self._retention_dirty or time.time() >= self._retention_sample_due:
                snapshot = await self.workers.read(lambda c, _t: JobRetention.snapshot(c))
                snapshot["needs_attention"] = snapshot["needs_attention"] or self._retention_attention
                self._retention_cache.update(snapshot, available=True, stale=False, cached_at=time.time())
                self._retention_sample_due = time.time() + 30
                self._retention_dirty = False
            else:
                self._retention_cache["terminal_counts"] = counts
                if self._retention_attention:
                    self._retention_cache["needs_attention"] = True
        except (McpError, OSError):
            self._retention_cache["stale"] = True
            raise
        else:
            return self._retention_cursor != (0.0, 0)

    async def _retention_loop(self) -> None:
        while not self._stopping:
            self._retention_event.clear()
            try:
                if await self.retention_pass():
                    await asyncio.sleep(0)
                    continue
            except (BusyError, LimitError):
                pass
            except (McpError, OSError):
                self._failure_cache("IO_ERROR: job retention failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._retention_event.wait(), timeout=30)

    async def _orphan_step(self, name: str) -> None:
        job_id, token, _suffix = name.split(".")
        bucket = await self.acquire_bucket("short", job_bucket(job_id), exclusive=True)
        try:
            disposable = await self.workers.control(lambda c, _t: staging_disposable(c, job_id, token))
            if disposable:
                await self.io("short", lambda: self.store.discard_stage(job_id, token))
                await self.workers.control(lambda c, _t: JobRetention.forget_clean_input(c, job_id, token))
        finally:
            os.close(bucket)

    async def _run_claim(self, claim: Claim) -> None:
        self._claims[claim.token] = claim
        heart = asyncio.create_task(self._heartbeat(claim))
        try:
            with self.events.job_step(claim.kind, claim.payload.get("_telemetry")) as observation:
                try:
                    claim.check()
                    if claim.kind == "ingest":
                        await self._ingest_step(claim)
                    elif claim.kind == "reindex":
                        await reindex_step(self, claim)
                    else:
                        await self._delete_step(claim)
                except (BusyError, LimitError) as exc:
                    observation.outcome = "error"
                    await self._defer_claim(claim, exc)
                except (McpError, OSError) as exc:
                    observation.outcome = "cancelled" if claim.cancelled else "error"
                    await self._fail_claim(claim, exc)
        finally:
            heart.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heart
            self._claims.pop(claim.token, None)
            self._retention_event.set()

    async def _defer_claim(self, claim: Claim, error: BusyError | LimitError) -> None:
        # Keep the heartbeat and input ownership during backoff. Wake notifications
        # cannot bypass this delay; shutdown can, leaving the lease to expire.
        delay = error.retry_after_ms / 1000 if isinstance(error, WalBusyError) else 1.0
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        if self._stopping or claim.lost:
            return

        if claim.stage_created and claim.token != claim.payload.get("input_token"):

            def discard() -> None:
                claim.check()
                self.store.discard_stage(claim.job_id, claim.token)

            with contextlib.suppress(McpError, OSError):
                await self.workers.control(lambda c, _t: JobStore.fence(c, claim))
                await self.io(claim.lane, discard)
                claim.stage_created = False

        def release(connection: apsw.Connection, _token: OperationToken) -> None:
            row = JobStore.fence(connection, claim)
            # A batch may have committed since the in-memory claim was loaded.
            JobStore.release(connection, claim, json.loads(row["progress"]))

        # Reset gates still apply. An unavailable control lane leaves the durable
        # lease intact for normal expiry/recovery, never a terminal pressure error.
        with contextlib.suppress(BusyError, LimitError, ConflictError):
            await self.workers.control(release)

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
        if claim.progress.get("awaiting_text_index"):
            await self._continue_ingest_index(claim)
            return
        claim.progress = await self.workers.control(lambda c, _t: row_progress(JobStore.fence(c, claim)))
        verified = None
        if "verified_sha256" in claim.progress:
            # The durable outstanding locator protects reuse from orphan cleanup;
            # only the final identity recheck and publication hold the bucket.
            verified = await self.io(
                claim.lane,
                lambda: self.store.verify_blob(claim.progress["verified_sha256"], claim.progress["bytes"], claim.check),
            )
        staged = None
        if verified is not None:
            digest, byte_size = verified.sha256, verified.byte_size
        else:
            staged = await self._copy_input(claim)
            claim.stage_created = True
            digest, byte_size = staged.sha256, staged.byte_size
            progress = {"bytes": byte_size, "chunks": 0, "verified_sha256": digest, "stage_token": claim.token}
            await self.workers.control(lambda c, _t: JobStore.checkpoint(c, claim, progress))
        bucket = await self.acquire_bucket(claim.lane, digest, exclusive=True)
        try:
            await self.workers.write(lambda c, _t: EvidenceRecords.check_existing(c, claim, digest))
            claim.check()
            if staged is not None:
                await self.io(claim.lane, lambda: self.store.publish(staged))
                claim.stage_created = False
            elif verified is not None:
                await self.io(claim.lane, lambda: self.store.recheck_blob(verified))
            claim.check()
            await self.workers.write(lambda c, _t: EvidenceRecords.publish_record(c, claim, digest, byte_size))
        finally:
            os.close(bucket)
        if "input_token" in options and len(self._orphans) < 32:
            self._orphans.append(stage_name(claim.job_id, options["input_token"]))
            self.wake("short")

    async def _continue_ingest_index(self, claim: Claim) -> None:
        try:
            result = await index_evidence(self, claim, claim.progress["evidence_id"])
        except ConflictError as exc:
            if str(exc) != "INDEX_BUSY" or not claim.progress.get("deduplicated"):
                raise
            await self.workers.write(
                lambda c, _t: EvidenceRecords.finish_dedup(c, claim, claim.progress["evidence_id"])
            )
            return
        result["warnings"] = [*claim.result.get("warnings", []), *result.get("warnings", [])]
        await self.workers.write(lambda c, _t: JobStore.finish(c, claim, "completed", {**claim.result, **result}))

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
            if len(content) != options.get("input_size") or hashlib.sha256(content).hexdigest() != options.get(
                "input_sha256"
            ):
                raise StorageIOError("IO_ERROR: admitted input fingerprint mismatch or unavailable")
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
                **claim.result,
                **(
                    {"index_state": "index_failed", "incomplete": True}
                    if claim.progress.get("awaiting_text_index")
                    else {}
                ),
                "error": error.error_type if isinstance(error, McpError) else "IO_ERROR",
                "reason": str(error) if isinstance(error, McpError) else "managed I/O failed",
            }
            if isinstance(error, RecordConflictError):
                result["details"] = error.details.model_dump(mode="json")
            await self.workers.control(lambda c, _t: JobStore.finish_failure(c, claim, state, result))
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
            if not items:
                raise LimitError("job item exceeds response budget")
            more = True
            break
        items.append(item)
        last_id = row[0]
    return {"jobs": items, "next_cursor": binding.encode(last_id) if more else None}


async def _settle(task: asyncio.Future[T]) -> T:
    while not task.done():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(task)
    return task.result()


def safe_background_error(message: str | None) -> str | None:
    """Expose only the fixed diagnostics authored by background failure sites."""
    if message is None:
        return None
    allowed = (
        "IO_ERROR: periodic job recovery failed",
        "IO_ERROR: background job admission failed",
        "IO_ERROR: job retention purge needs attention",
        "IO_ERROR: retention ownership metadata needs attention",
        "IO_ERROR: job retention failed",
        "IO_ERROR: job failure could not be committed",
    )
    return message if message in allowed else "IO_ERROR: background job failure"
