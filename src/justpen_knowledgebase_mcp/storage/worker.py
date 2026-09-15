"""Bounded owner-thread SQLite execution with generation-safe interruption."""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypeVar

import apsw

from ..errors import BusyError, CancelledOperationError, InternalError, LimitError, McpError, StorageIOError

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from .connection import SQLiteRuntime

T = TypeVar("T")
State = Literal["queued", "running", "committing", "done"]
Lane = Literal["read", "write", "control"]


@dataclass
class OperationToken:
    """One operation's absolute budget and lifecycle; never reuse a submitted token."""

    deadline: float
    cancelled: bool = False
    generation: int = 0
    state: State = "queued"
    submitted: bool = False

    def check(self) -> None:
        """Check cancellation and the shared queue/lock/query budget."""
        if self.cancelled:
            raise CancelledOperationError("operation cancelled before commit")
        if time.monotonic() >= self.deadline:
            raise LimitError("operation deadline exceeded")


@dataclass
class _Work:
    callback: Callable[[apsw.Connection, OperationToken], Any]
    token: OperationToken
    future: asyncio.Future[Any]


@dataclass
class _Owner:
    reader: bool
    ready: asyncio.Future[None]
    closed: asyncio.Future[None]
    connection: apsw.Connection | None = None
    current: OperationToken | None = None
    generation: int = 0
    thread: threading.Thread | None = None


class DatabaseWorkers:
    """One writer/control owner and a fixed pool sharing one bounded read queue.

    Callbacks are internal synchronous DB operations. Materialize query results
    before returning; never return cursors or connections. Evidence/file I/O and
    filesystem-lock waits belong outside callbacks and their transactions.
    """

    def __init__(self, factory: SQLiteRuntime) -> None:
        """Allocate bounded queues; no threads or files are opened here."""
        self.factory = factory
        self._condition = threading.Condition(threading.RLock())
        self._queues: dict[Lane, deque[_Work]] = {lane: deque() for lane in ("read", "write", "control")}
        self._owners: list[_Owner] = []
        self._stopping = False
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        """Initialize writer before reader connections, each on its owner thread."""
        self._loop = asyncio.get_running_loop()
        try:
            for reader in [False] + [True] * self.factory.config.db_reader_threads:
                owner = _Owner(reader, self._loop.create_future(), self._loop.create_future())
                self._owners.append(owner)
                owner.thread = threading.Thread(
                    target=self._run, args=(owner,), name="kb-reader" if reader else "kb-writer"
                )
                owner.thread.start()
                await asyncio.shield(owner.ready)
        except BaseException:
            await self.close()
            raise

    async def read(
        self, callback: Callable[[apsw.Connection, OperationToken], T], token: OperationToken | None = None
    ) -> T:
        """Run a callback in a guarded query-only snapshot."""
        return await self._submit("read", callback, token)

    async def write(
        self, callback: Callable[[apsw.Connection, OperationToken], T], token: OperationToken | None = None
    ) -> T:
        """Run validation and mutation after BEGIN IMMEDIATE and the schema guard."""
        return await self._submit("write", callback, token)

    async def control(
        self, callback: Callable[[apsw.Connection, OperationToken], T], token: OperationToken | None = None
    ) -> T:
        """Run a short guarded write on the writer's bounded priority lane."""
        return await self._submit("control", callback, token)

    async def _submit(
        self, lane: Lane, callback: Callable[[apsw.Connection, OperationToken], T], token: OperationToken | None
    ) -> T:
        token = token or OperationToken(time.monotonic() + self.factory.config.query_timeout_ms / 1000)
        token.check()
        with self._condition:
            if self._stopping or len(self._queues[lane]) >= (16 if lane == "control" else 128):
                raise BusyError("database queue unavailable")
            if token.submitted:
                raise ValueError("operation token already submitted")
            token.submitted = True
            future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
            self._queues[lane].append(_Work(callback, token, future))
            self._condition.notify_all()
        try:
            async with asyncio.timeout(max(0, token.deadline - time.monotonic())):
                return await asyncio.shield(future)
        except (TimeoutError, asyncio.CancelledError) as interruption:
            accepted = self.interrupt_if_current(token)
            # A commit that won the lifecycle lock cannot truthfully be reported cancelled.
            if not accepted:
                return await _committed_result(future)
            # Retrieve eventual rollback/error even when the caller has departed.
            future.add_done_callback(_consume_result)
            if isinstance(interruption, TimeoutError):
                raise LimitError("operation deadline exceeded") from None
            raise

    def interrupt_if_current(self, token: OperationToken) -> bool:
        """Accept precommit cancellation and interrupt only its live generation."""
        with self._condition:
            if token.state in ("committing", "done"):
                return False
            token.cancelled = True
            for owner in self._owners:
                if owner.current is token and owner.generation == token.generation and owner.connection is not None:
                    owner.connection.interrupt()
            self._condition.notify_all()
            return True

    async def close(self) -> None:
        """Stop admission, cancel queued/running work, and await owner-thread close."""
        with self._condition:
            self._stopping = True
            for queue in self._queues.values():
                for work in queue:
                    self.interrupt_if_current(work.token)
            for owner in self._owners:
                if owner.current is not None:
                    self.interrupt_if_current(owner.current)
            self._condition.notify_all()
        if self._owners:
            results = await asyncio.shield(
                asyncio.gather(*(owner.closed for owner in self._owners), return_exceptions=True)
            )
            if any(isinstance(result, BaseException) for result in results):
                raise StorageIOError("database close failed")

    def _notify(self, future: asyncio.Future[Any], value: object = None, error: BaseException | None = None) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(_complete, future, value, error)

    def _next(self, owner: _Owner, streak: int) -> tuple[_Work | None, int]:
        with self._condition:
            while True:
                if owner.reader:
                    lane: Lane = "read"
                else:
                    lane = (
                        "control" if self._queues["control"] and (streak < 4 or not self._queues["write"]) else "write"
                    )
                if self._queues[lane]:
                    work = self._queues[lane].popleft()
                    owner.generation += 1
                    owner.current = work.token
                    work.token.generation = owner.generation
                    work.token.state = "running"
                    return work, streak + 1 if lane == "control" else 0
                if self._stopping:
                    return None, streak
                self._condition.wait()

    def _run(self, owner: _Owner) -> None:
        connection: apsw.Connection | None = None
        outcome = _Outcome()
        work: _Work | None = None
        with outcome:
            connection = self.factory.open_reader() if owner.reader else self.factory.open_writer()
            with self._condition:
                owner.connection = connection
            self._notify(owner.ready)
            streak = 0
            while True:
                work, streak = self._next(owner, streak)
                if work is None:
                    break
                self._execute(owner, connection, work)
        if outcome.error is not None:
            self._notify(owner.ready, error=outcome.error)
            if work is not None:
                error = _public_error(outcome.error, work.token)
                with self._condition:
                    work.token.state = "done"
                self._notify(work.future, error=error)
            self._fail_queued()
        # Mark unavailable under the same lock used by interrupt, before native close.
        with self._condition:
            owner.connection = None
            owner.current = None
        closed = _Outcome()
        with closed:
            if connection is not None:
                connection.close()
        self._notify(owner.closed, error=closed.error)

    def _fail_queued(self) -> None:
        with self._condition:
            self._stopping = True
            for queue in self._queues.values():
                while queue:
                    work = queue.popleft()
                    work.token.state = "done"
                    self._notify(work.future, error=StorageIOError("database owner unavailable"))
            self._condition.notify_all()

    def _execute(self, owner: _Owner, connection: apsw.Connection, work: _Work) -> None:
        token = work.token
        busy_started: float | None = None

        def busy(_count: int) -> bool:
            nonlocal busy_started
            now = time.monotonic()
            busy_started = now if busy_started is None else busy_started
            remaining = min(token.deadline - now, self.factory.config.db_busy_timeout_ms / 1000 - (now - busy_started))
            if token.cancelled or remaining <= 0:
                return False
            time.sleep(min(0.005, remaining))
            return True

        def progress() -> bool:
            return token.state != "committing" and (token.cancelled or time.monotonic() >= token.deadline)

        connection.set_busy_handler(busy)
        connection.set_progress_handler(progress, 1000)
        result: Any = None
        outcome = _Outcome()
        with outcome:
            token.check()
            connection.execute("BEGIN" if owner.reader else "BEGIN IMMEDIATE")
            self.factory.guard.check(connection)
            result = work.callback(connection, token)
            with self._condition:
                token.check()
                token.state = "committing"
            connection.execute("COMMIT")
        error = _public_error(outcome.error, token) if outcome.error is not None else None
        # Cleanup cannot be interrupted by a second cancellation notification.
        # The owner does not dequeue/reuse this connection until cleanup finishes.
        with self._condition:
            owner.current = None
        # Interrupted INSERT/UPDATE can already have rolled back the transaction.
        connection.set_progress_handler(None)
        cleanup = _Outcome()
        with cleanup:
            if not connection.get_autocommit():
                connection.execute("ROLLBACK")
        connection.set_busy_handler(None)
        with self._condition:
            token.state = "done"
            owner.current = None
        self._notify(work.future, result, error)
        if cleanup.error is not None:
            # A connection whose rollback failed must not accept another operation.
            raise StorageIOError("database cleanup failed") from None


class _Outcome:
    """Transport arbitrary callback failures across the thread boundary without logging data."""

    def __init__(self) -> None:
        """Initialize an empty outcome."""
        self.error: BaseException | None = None

    def __enter__(self) -> _Outcome:
        """Enter the owner-thread exception transport boundary."""
        return self

    def __exit__(
        self, _kind: type[BaseException] | None, error: BaseException | None, _traceback: TracebackType | None
    ) -> bool:
        """Save errors for explicit delivery and cleanup by the worker."""
        self.error = error
        return error is not None


def _public_error(error: BaseException, token: OperationToken) -> BaseException:
    if isinstance(error, McpError):
        return error
    if token.state != "committing":
        if token.cancelled:
            return CancelledOperationError("operation cancelled before commit")
        if time.monotonic() >= token.deadline:
            return LimitError("operation deadline exceeded")
    if isinstance(error, apsw.BusyError):
        return BusyError("database lock unavailable")
    if isinstance(error, (apsw.IOError, OSError)):
        return StorageIOError("managed storage operation failed")
    return InternalError("database operation failed")


def _complete(future: asyncio.Future[Any], value: object, error: BaseException | None) -> None:
    if not future.done():
        if error is None:
            future.set_result(value)
        else:
            future.set_exception(error)


def _consume_result(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()


async def _committed_result(future: asyncio.Future[T]) -> T:
    # Once commit started, even repeated transport cancellation cannot assert
    # that the write was undone. Deliver the actual eventual commit outcome.
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            continue
    return future.result()
