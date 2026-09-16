"""Workspace-scoped service facade and resource lifetime."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import uuid4

from pydantic import ValidationError

from .errors import ExpectedValidationError, InvalidParamsError, LimitError
from .evidence import IngestRequest, ReadEvidenceRequest
from .jobs import JobRunner, JobsRequest, safe_background_error
from .models import DeleteRequest, GetRequest, NeighborsRequest, SearchRequest, TypesRequest, WriteRequest
from .reindex import ReindexRequest, admit_reindex
from .responses import bounded_response
from .shutdown import ShutdownObserver
from .status import Capabilities, StatusResult, StatusSampler
from .storage.connection import SQLiteRuntime
from .storage.graph import Graph, graph_types
from .storage.maintenance import CheckpointMaintenance
from .storage.search import search
from .storage.traversal import neighbors
from .storage.worker import DatabaseWorkers, OperationToken
from .telemetry.context import capture_job_context
from .workspace import WorkspacePaths

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from contextlib import AbstractContextManager

    from .config import ServerConfig
    from .telemetry.events import TelemetryEvents

T = TypeVar("T")


@dataclass
class KnowledgeBase:
    """Own config, pinned workspace, connection factory and bounded DB workers."""

    config: ServerConfig
    workspace: WorkspacePaths
    workers: DatabaseWorkers
    maintenance: CheckpointMaintenance
    job_runner: JobRunner
    status_sampler: StatusSampler

    async def status(self) -> dict[str, Any]:
        """Return only bounded cached status; never admit database work here."""
        wal = self.maintenance.status()
        # Cached admission category, not a new physical measurement or promise.
        wal["reason"] = (
            "RESET_PENDING" if wal["phase"] == "reset" else None if wal["phase"] == "normal" else "WAL_PRESSURE"
        )
        result = StatusResult.model_validate(
            {
                "bind_scope": "stdio"
                if self.config.transport == "stdio"
                else "loopback"
                if self.config.is_loopback
                else "non_loopback",
                "allowed_hosts": []
                if self.config.transport == "stdio"
                else [self.config.host, *self.config.allowed_hosts],
                "background_error": safe_background_error(self.job_runner.last_error),
                "database": self.status_sampler.snapshot(),
                "wal": wal,
                "retention": self.job_runner.retention_status(),
                "database_queues": self.workers.queue_status(),
                "io_queues": self.job_runner.queue_status(),
                "capabilities": Capabilities(
                    query_timeout_ms=self.config.query_timeout_ms,
                    db_busy_timeout_ms=self.config.db_busy_timeout_ms,
                    db_reader_threads=self.config.db_reader_threads,
                ),
            }
        )
        return bounded_response(result.model_dump(mode="json"))

    async def ingest_evidence(self, request: IngestRequest | dict[str, Any]) -> dict[str, Any]:
        """Accept raw evidence durably and wait for bounded short storage work."""
        deadline = time.monotonic() + self.config.query_timeout_ms / 1000
        try:
            validated = await self.job_runner.io("short", lambda: IngestRequest.model_validate(request), deadline)
        except ValueError as exc:
            raise InvalidParamsError("invalid evidence source") from exc
        OperationToken(deadline).check()
        return await self.job_runner.ingest(validated, deadline)

    async def reindex(self, request: ReindexRequest | dict[str, Any]) -> dict[str, Any]:
        """Accept a fenced rebuild using the existing durable job runner."""
        deadline = time.monotonic() + self.config.query_timeout_ms / 1000
        try:
            validated = ReindexRequest.model_validate(request)
        except ValueError as exc:
            raise InvalidParamsError("invalid reindex request") from exc
        initiating_context = capture_job_context()
        result = await self.workers.write(
            lambda c, _t: admit_reindex(c, validated, str(uuid4()), initiating_context=initiating_context),
            OperationToken(deadline),
        )
        self.job_runner.wake(result["lane"])
        return bounded_response(result)

    async def read_evidence(self, request: ReadEvidenceRequest | dict[str, Any]) -> dict[str, Any]:
        """Return a bounded exact byte range from ready owned evidence."""
        deadline = time.monotonic() + self.config.query_timeout_ms / 1000
        try:
            validated = ReadEvidenceRequest.model_validate(request)
        except ValueError as exc:
            raise InvalidParamsError("invalid evidence range") from exc
        return await self.job_runner.read(validated, deadline)

    async def delete(self, request: DeleteRequest | dict[str, Any]) -> dict[str, Any]:
        """Accept one atomic batch and run bounded durable cleanup steps."""
        deadline = time.monotonic() + self.config.query_timeout_ms / 1000
        try:
            validated = DeleteRequest.model_validate(request)
        except ValueError as exc:
            raise InvalidParamsError("invalid delete request") from exc
        return await self.job_runner.delete(validated, deadline)

    async def jobs(self, request: JobsRequest | dict[str, Any]) -> dict[str, Any]:
        """Read and control bounded durable job metadata."""
        deadline = time.monotonic() + self.config.query_timeout_ms / 1000
        try:
            validated = JobsRequest.model_validate(request)
        except ValueError as exc:
            raise InvalidParamsError("invalid job operation") from exc
        return await self.job_runner.control(validated, deadline)

    async def write(self, request: WriteRequest | dict[str, Any]) -> dict[str, Any]:
        """Atomically merge a validated graph batch in the admitted writer."""
        deadline = time.monotonic() + self.config.query_timeout_ms / 1000
        try:
            validated = WriteRequest.model_validate(request)
        except ValueError as exc:
            if isinstance(exc, ValidationError):
                for detail in exc.errors(include_input=False, include_url=False):
                    cause = detail.get("ctx", {}).get("error")
                    if isinstance(cause, ExpectedValidationError):
                        raise InvalidParamsError(cause.message) from None
            raise InvalidParamsError("invalid write request") from None
        return await self.workers.write(
            lambda connection, token: Graph.write(connection, token, validated), OperationToken(deadline)
        )

    async def get(self, request: GetRequest | dict[str, Any]) -> dict[str, Any]:
        """Read bounded canonical records in one guarded snapshot."""
        deadline = time.monotonic() + self.config.query_timeout_ms / 1000
        try:
            validated = GetRequest.model_validate(request)
        except ValueError as exc:
            raise InvalidParamsError("invalid get request") from exc
        return await self.workers.read(
            lambda connection, token: Graph.get(connection, token, validated), OperationToken(deadline)
        )

    async def neighbors(self, request: NeighborsRequest | dict[str, Any]) -> dict[str, Any]:
        """Traverse ready adjacency within explicit output budgets."""
        deadline = time.monotonic() + self.config.query_timeout_ms / 1000
        try:
            validated = NeighborsRequest.model_validate(request)
        except ValueError as exc:
            raise InvalidParamsError("invalid traversal request") from exc
        return await self.workers.read(
            lambda connection, token: neighbors(connection, token, validated), OperationToken(deadline)
        )

    async def search(self, request: SearchRequest | dict[str, Any]) -> dict[str, Any]:
        """Select records with exact filters and verified text in one bounded snapshot."""
        deadline = time.monotonic() + self.config.query_timeout_ms / 1000
        try:
            validated = SearchRequest.model_validate(request)
        except ValueError as exc:
            raise InvalidParamsError("invalid search request") from exc
        try:
            return await self.workers.read(
                lambda connection, token: search(connection, token, validated), OperationToken(deadline)
            )
        except LimitError as exc:
            raise LimitError("search incomplete: operation budget exceeded") from exc

    async def types(self, request: TypesRequest | dict[str, Any]) -> dict[str, Any]:
        """Discover controlled types with ready-only snapshot counts."""
        deadline = time.monotonic() + self.config.query_timeout_ms / 1000
        try:
            validated = TypesRequest.model_validate(request)
        except ValueError as exc:
            raise InvalidParamsError("invalid types request") from exc
        return await self.workers.read(
            lambda connection, token: graph_types(connection, token, validated), OperationToken(deadline)
        )

    @classmethod
    @asynccontextmanager
    async def open(
        cls,
        config: ServerConfig,
        *,
        runtime_context: Callable[[WorkspacePaths], AbstractContextManager[None]] | None = None,
        _shutdown_observer: ShutdownObserver | None = None,
        _telemetry_events: TelemetryEvents | None = None,
    ) -> AsyncGenerator[KnowledgeBase]:
        """Open resources at application lifespan entry and close in owner order."""
        observer = _shutdown_observer or ShutdownObserver()
        try:
            async with _off_loop_resource(
                lambda: WorkspacePaths(config), lambda workspace: workspace.close()
            ) as workspace:
                with runtime_context(workspace) if runtime_context is not None else nullcontext():
                    async with _off_loop_resource(
                        lambda: SQLiteRuntime(workspace, config), lambda factory: factory.close()
                    ) as factory:
                        workers = DatabaseWorkers(factory)
                        maintenance = CheckpointMaintenance(factory)
                        job_runner = None
                        status_sampler = StatusSampler(workers)
                        try:
                            await maintenance.start()
                            await workers.start()
                            policy = await workers.control(lambda connection, _token: factory.guard.policy(connection))
                            job_runner = JobRunner(workers, workspace, policy)
                            if _telemetry_events is not None:
                                job_runner.events = _telemetry_events
                            await job_runner.start()
                            await status_sampler.start()
                            yield cls(config, workspace, workers, maintenance, job_runner, status_sampler)
                        finally:
                            await _close_workers(workers, observer, maintenance, job_runner, status_sampler)
        finally:
            if _shutdown_observer is None:
                await observer.close()


@asynccontextmanager
async def _off_loop_resource(create: Callable[[], T], dispose: Callable[[T], None]) -> AsyncGenerator[T]:
    # Cancellation cannot abandon native initialization or its resulting resource.
    resource, cancelled = await _drain_owned(asyncio.create_task(asyncio.to_thread(create)))
    try:
        if cancelled:
            raise asyncio.CancelledError
        yield resource
    finally:
        _, cancelled = await _drain_owned(asyncio.create_task(asyncio.to_thread(dispose, resource)))
        if cancelled:
            raise asyncio.CancelledError


async def _drain_owned(task: asyncio.Task[T]) -> tuple[T, bool]:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


async def _close_workers(
    workers: DatabaseWorkers,
    observer: ShutdownObserver,
    maintenance: CheckpointMaintenance,
    job_runner: JobRunner | None = None,
    status_sampler: StatusSampler | None = None,
) -> None:
    # EOF/startup unwind may be the first trigger; a CLI signal may already have
    # started this same observer while transport teardown was still pending.
    observer.start()

    async def close_all() -> None:
        try:
            try:
                if status_sampler is not None:
                    await status_sampler.close()
            finally:
                if job_runner is not None:
                    await job_runner.close()
        finally:
            try:
                await maintenance.close()
            finally:
                await workers.close()

    cleanup = asyncio.create_task(close_all())
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # Repeated cancellation must not release VFS/descriptors early.
            cancelled = True
    cleanup.result()
    if cancelled:
        raise asyncio.CancelledError
