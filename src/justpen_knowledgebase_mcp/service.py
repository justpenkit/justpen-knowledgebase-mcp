"""Workspace-scoped service facade and resource lifetime."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING

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
    ) -> AsyncGenerator[KnowledgeBase]:
        """Open resources at application lifespan entry and close in owner order."""
        workspace = WorkspacePaths(config)
        try:
            with runtime_context(workspace) if runtime_context is not None else nullcontext():
                factory = SQLiteRuntime(workspace, config)
                workers = DatabaseWorkers(factory)
                try:
                    await workers.start()
                    yield cls(config, workspace, workers)
                finally:
                    cleanup = asyncio.create_task(workers.close())
                    # Repeated transport cancellation cannot unregister the VFS
                    # or release pinned descriptors while an owner still uses them.
                    cancelled = False
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            cancelled = True
                    try:
                        cleanup.result()
                    finally:
                        factory.close()
                    if cancelled:
                        raise asyncio.CancelledError
        finally:
            workspace.close()
