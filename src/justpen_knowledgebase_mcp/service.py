"""Workspace-scoped service facade and resource lifetime."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .errors import InvalidParamsError, LimitError
from .evidence import IngestRequest, ReadEvidenceRequest
from .jobs import JobRunner, JobsRequest
from .models import DeleteRequest, NeighborsRequest, SearchRequest
from .reindex import ReindexRequest, admit_reindex
from .responses import bounded_response
from .shutdown import ShutdownObserver
from .storage.connection import SQLiteRuntime
from .storage.graph import Graph, graph_types
from .storage.maintenance import CheckpointMaintenance
from .storage.search import search
from .storage.traversal import neighbors
from .storage.worker import DatabaseWorkers, OperationToken
from .workspace import WorkspacePaths

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from contextlib import AbstractContextManager

    from .config import ServerConfig
    from .models import GetRequest, TypesRequest, WriteRequest


@dataclass
class KnowledgeBase:
    """Own config, pinned workspace, connection factory and bounded DB workers."""

    config: ServerConfig
    workspace: WorkspacePaths
    workers: DatabaseWorkers
    maintenance: CheckpointMaintenance
    job_runner: JobRunner

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
        result = await self.workers.write(
            lambda c, _t: admit_reindex(c, validated, str(uuid4())), OperationToken(deadline)
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

    async def delete(self, request: DeleteRequest) -> dict[str, Any]:
        """Accept one atomic batch and run bounded durable cleanup steps."""
        deadline = time.monotonic() + self.config.query_timeout_ms / 1000
        return await self.job_runner.delete(request, deadline)

    async def jobs(self, request: JobsRequest | dict[str, Any]) -> dict[str, Any]:
        """Read and control bounded durable job metadata."""
        deadline = time.monotonic() + self.config.query_timeout_ms / 1000
        try:
            validated = JobsRequest.model_validate(request)
        except ValueError as exc:
            raise InvalidParamsError("invalid job operation") from exc
        return await self.job_runner.control(validated, deadline)

    async def write(self, request: WriteRequest) -> dict[str, Any]:
        """Atomically merge a validated graph batch in the admitted writer."""
        return await self.workers.write(lambda connection, token: Graph.write(connection, token, request))

    async def get(self, request: GetRequest) -> dict[str, Any]:
        """Read bounded canonical records in one guarded snapshot."""
        return await self.workers.read(lambda connection, token: Graph.get(connection, token, request))

    async def neighbors(self, request: NeighborsRequest | dict[str, Any]) -> dict[str, Any]:
        """Traverse ready adjacency within explicit output budgets."""
        try:
            validated = NeighborsRequest.model_validate(request)
        except ValueError as exc:
            raise InvalidParamsError("invalid traversal request") from exc
        return await self.workers.read(lambda connection, token: neighbors(connection, token, validated))

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

    async def types(self, request: TypesRequest) -> dict[str, Any]:
        """Discover controlled types with ready-only snapshot counts."""
        return await self.workers.read(lambda connection, token: graph_types(connection, token, request))

    @classmethod
    @asynccontextmanager
    async def open(
        cls,
        config: ServerConfig,
        *,
        runtime_context: Callable[[WorkspacePaths], AbstractContextManager[None]] | None = None,
        _shutdown_observer: ShutdownObserver | None = None,
    ) -> AsyncGenerator[KnowledgeBase]:
        """Open resources at application lifespan entry and close in owner order."""
        observer = _shutdown_observer or ShutdownObserver()
        workspace = WorkspacePaths(config)
        try:
            with runtime_context(workspace) if runtime_context is not None else nullcontext():
                factory = SQLiteRuntime(workspace, config)
                workers = DatabaseWorkers(factory)
                maintenance = CheckpointMaintenance(factory)
                job_runner = None
                try:
                    await maintenance.start()
                    await workers.start()
                    policy = await workers.read(lambda connection, _token: factory.guard.policy(connection))
                    job_runner = JobRunner(workers, workspace, policy)
                    await job_runner.start()
                    yield cls(config, workspace, workers, maintenance, job_runner)
                finally:
                    try:
                        await _close_workers(workers, observer, maintenance, job_runner)
                    finally:
                        factory.close()
        finally:
            workspace.close()
            if _shutdown_observer is None:
                await observer.close()


async def _close_workers(
    workers: DatabaseWorkers,
    observer: ShutdownObserver,
    maintenance: CheckpointMaintenance,
    job_runner: JobRunner | None = None,
) -> None:
    # EOF/startup unwind may be the first trigger; a CLI signal may already have
    # started this same observer while transport teardown was still pending.
    observer.start()

    async def close_all() -> None:
        try:
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
