"""Workspace-scoped service facade and resource lifetime."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .shutdown import ShutdownObserver
from .storage.connection import SQLiteRuntime
from .storage.graph import Graph, graph_types
from .storage.maintenance import CheckpointMaintenance
from .storage.worker import DatabaseWorkers
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

    async def write(self, request: WriteRequest) -> dict[str, Any]:
        """Atomically merge a validated graph batch in the admitted writer."""
        return await self.workers.write(lambda connection, token: Graph.write(connection, token, request))

    async def get(self, request: GetRequest) -> dict[str, Any]:
        """Read bounded canonical records in one guarded snapshot."""
        return await self.workers.read(lambda connection, token: Graph.get(connection, token, request))

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
                try:
                    await maintenance.start()
                    await workers.start()
                    yield cls(config, workspace, workers, maintenance)
                finally:
                    try:
                        await _close_workers(workers, observer, maintenance)
                    finally:
                        factory.close()
        finally:
            workspace.close()
            if _shutdown_observer is None:
                await observer.close()


async def _close_workers(
    workers: DatabaseWorkers, observer: ShutdownObserver, maintenance: CheckpointMaintenance
) -> None:
    # EOF/startup unwind may be the first trigger; a CLI signal may already have
    # started this same observer while transport teardown was still pending.
    observer.start()

    async def close_all() -> None:
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
