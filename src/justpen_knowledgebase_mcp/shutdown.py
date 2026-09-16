"""One internal grace observer shared by transport and lifespan shutdown."""

from __future__ import annotations

import asyncio
import logging
import os

SHUTDOWN_GRACE_SECONDS = 30.0


class ShutdownObserver:
    """Observe one shutdown episode without cancelling native cleanup."""

    def __init__(self) -> None:
        """Allocate no background task before a shutdown trigger."""
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start once at the earliest trigger; later calls never reset the budget."""
        if self._task is None:
            deadline = asyncio.get_running_loop().time() + SHUTDOWN_GRACE_SECONDS
            self._task = asyncio.create_task(self._observe(deadline), name="kb-shutdown-observer")

    async def _observe(self, deadline: float) -> None:
        await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))
        logging.getLogger(__name__).error(
            "shutdown_timeout: pid=%s; supervisor must kill if cleanup cannot finish", os.getpid()
        )

    async def close(self) -> None:
        """Retire the observer after all transport/lifespan cleanup finishes."""
        if self._task is None:
            return
        self._task.cancel()
        cancelled = False
        while not self._task.done():
            try:
                await asyncio.wait({self._task})
            except asyncio.CancelledError:
                cancelled = True
        if not self._task.cancelled():
            self._task.result()
        if cancelled:
            raise asyncio.CancelledError
