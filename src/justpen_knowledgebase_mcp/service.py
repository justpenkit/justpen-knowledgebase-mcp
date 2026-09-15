"""Workspace-scoped service facade and resource lifetime."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .shutdown import ShutdownObserver
from .storage.connection import SQLiteRuntime
from .storage.worker import DatabaseWorkers
from .workspace import WorkspacePaths

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from contextlib import AbstractContextManager

    from .config import ServerConfig


@dataclass
class KnowledgeBase:
    """Own config, pinned workspace, connection factory and bounded DB workers."""

    config: ServerConfig
    workspace: WorkspacePaths
    workers: DatabaseWorkers

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
                try:
                    await workers.start()
                    yield cls(config, workspace, workers)
                finally:
                    try:
                        await _close_workers(workers, observer)
                    finally:
                        factory.close()
        finally:
            workspace.close()
            if _shutdown_observer is None:
                await observer.close()


async def _close_workers(workers: DatabaseWorkers, observer: ShutdownObserver) -> None:
    # EOF/startup unwind may be the first trigger; a CLI signal may already have
    # started this same observer while transport teardown was still pending.
    observer.start()
    cleanup = asyncio.create_task(workers.close())
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
