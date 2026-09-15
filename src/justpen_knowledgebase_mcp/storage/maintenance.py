"""Workspace-wide checkpoint attempts and process-local bounded status snapshots."""

from __future__ import annotations

import asyncio
import fcntl
import math
import os
import threading
import time
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import apsw
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..errors import BusyError, LimitError, PathDeniedError, StorageIOError
from .admission import open_lock
from .worker import OperationToken, OwnerOutcome

if TYPE_CHECKING:
    from ..config import WorkspacePolicy
    from ..workspace import WorkspacePaths
    from .connection import ManagedConnection, SQLiteRuntime

Trigger = Literal["timer", "low", "startup", "stale", "pressure"]
Phase = Literal["normal", "pressure", "assessment_pending", "unknown"]


class WalState(BaseModel):
    """Persisted measurements and diagnostics; timestamps are not liveness proof."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)
    phase: Phase = "unknown"
    sample_seq: int = Field(default=0, ge=0)
    sample_at: float | None = None
    allocated_bytes: int | None = Field(default=None, ge=0)
    log_frames: int | None = Field(default=None, ge=0)
    checkpointed_frames: int | None = Field(default=None, ge=0)
    page_size: int | None = Field(default=None, gt=0)
    unbackfilled_bytes: int | None = Field(default=None, ge=0)
    pressure_started_at: float | None = None
    attempt_id: str | None = None
    attempt_started_at: float | None = None
    attempt_finished_at: float | None = None
    next_attempt_not_before: float | None = None
    checkpoint_mode: str | None = None
    last_attempt: str | None = None

    def valid_sample(self, now: float) -> bool:
        """Future, incomplete and inconsistent samples cannot admit products."""
        return (
            self.sample_seq > 0
            and self.sample_at is not None
            and 0 <= self.sample_at <= now
            and self.allocated_bytes is not None
            and self.log_frames is not None
            and self.checkpointed_frames is not None
            and self.page_size is not None
            and self.checkpointed_frames <= self.log_frames
            and self.unbackfilled_bytes == (self.log_frames - self.checkpointed_frames) * self.page_size
        )

    def retry_after_ms(self, now: float) -> int:
        """Bound shared retry advice to the public envelope; never estimate finish."""
        remaining = max(0, (self.next_attempt_not_before or now) - now)
        return min(30000, max(1000, math.ceil(remaining * 1000)))

    @classmethod
    def read(cls, connection: apsw.Connection) -> WalState:
        """Read shared state inside the caller's guarded transaction."""
        raw = connection.execute("SELECT maintenance FROM settings WHERE singleton=1").get
        try:
            return cls.model_validate_json(raw)
        except (ValidationError, TypeError):
            return cls()


def allocation(workspace: WorkspacePaths) -> int | None:
    """Measure WAL allocation; absent WAL is zero, failed measurement unknown."""
    try:
        return workspace.validate_native(str(workspace.db) + "-wal").stat().st_size
    except FileNotFoundError:
        return 0
    except (OSError, PathDeniedError, StorageIOError):
        return None


class StatusCache:
    """Atomic process-local snapshot; status never acquires SQL or file locks."""

    def __init__(self) -> None:
        """Start unavailable until a shared snapshot is observed."""
        self._lock = threading.Lock()
        self._state = WalState()
        self._cached_at: float | None = None
        self._reset = False
        self._evaluation_requested = False

    def update(self, state: WalState) -> None:
        """Copy the immutable snapshot after shared reads or committed publication."""
        with self._lock:
            self._state = state
            self._cached_at = time.time()
            if state.phase == "normal" and state.valid_sample(self._cached_at):
                self._evaluation_requested = False

    def request(self) -> None:
        """Report pending evaluation without inventing a shared measurement."""
        with self._lock:
            self._evaluation_requested = True

    def reset(self, *, active: bool) -> None:
        """Annotate the local reset while SQL is deliberately unavailable."""
        with self._lock:
            self._reset = active

    def snapshot(self) -> dict[str, object]:
        """Return bounded sample age and retry advice, never completion estimates."""
        now = time.time()
        with self._lock:
            state, cached, reset, requested = self._state, self._cached_at, self._reset, self._evaluation_requested
        valid = state.valid_sample(now)
        age = now - state.sample_at if valid and state.sample_at is not None else None
        stale = state.phase == "normal" and age is not None and age > 35
        phase = state.phase if valid or state.phase != "normal" else "unknown"
        result: dict[str, object] = state.model_dump()
        result.update(
            phase="reset" if reset else phase,
            sample_age=age,
            cache_age=None if cached is None else max(0, now - cached),
            stale_normal=stale,
            unavailable=not valid,
            evaluation_requested=requested or stale,
            pressure_elapsed=None if state.pressure_started_at is None else max(0, now - state.pressure_started_at),
            retry_after_ms=state.retry_after_ms(now),
            estimated_completion_ms=None,
        )
        return result


class CheckpointMaintenance:
    """One dedicated owner thread; checkpoint.lock is held only during attempts."""

    def __init__(self, factory: SQLiteRuntime) -> None:
        """Allocate synchronization only; native resources open on the owner."""
        self.factory = factory
        self.connection: ManagedConnection | None = None
        self._leader_fd: int | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._trigger: Trigger = "startup"
        self._trigger_lock = threading.Lock()
        self._closed_error: BaseException | None = None
        self.factory.wake_maintenance = self.request

    def request(self, trigger: Trigger) -> None:
        """Wake locally using monotonic waits; cooldown stays shared wall-clock."""
        with self._trigger_lock:
            if trigger == "pressure" or self._trigger != "pressure":
                self._trigger = trigger
        self.factory.status_cache.request()
        self._wake.set()

    def status(self) -> dict[str, object]:
        """Serve status without database work during pressure, unknown or reset."""
        return self.factory.status_cache.snapshot()

    async def start(self) -> None:
        """Initialize on the maintenance thread and perform an immediate attempt."""
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()

        def run() -> None:
            outcome = OwnerOutcome()
            with outcome:
                self.run_once("startup")
                loop.call_soon_threadsafe(ready.set_result, None)
                while not self._stop.is_set():
                    snapshot = self.status()
                    retry = snapshot["phase"] != "normal" or snapshot["stale_normal"]
                    wait = 1.0 if retry else 30.0
                    if self._wake.wait(wait):
                        self._wake.clear()
                        with self._trigger_lock:
                            trigger = self._trigger
                            self._trigger = "timer"
                    else:
                        trigger = "pressure" if retry else "timer"
                    if not self._stop.is_set():
                        self.run_once(trigger)
            if outcome.error is not None:
                self._closed_error = outcome.error
                loop.call_soon_threadsafe(_startup_failed, ready, outcome.error)
            closed = OwnerOutcome()
            with closed:
                self.close_owner()
            self._closed_error = self._closed_error or closed.error

        self._thread = threading.Thread(target=run, name="kb-checkpoint")
        self._thread.start()
        await asyncio.shield(ready)

    async def close(self) -> None:
        """Stop wakes and await owner cleanup; native I/O is not force-closed."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join)
        if self._closed_error is not None:
            raise self._closed_error

    def close_owner(self) -> None:
        """Close native connection before its persistent leader descriptor."""
        outcome = OwnerOutcome()
        with outcome:
            if self.connection is not None:
                self.factory.close_connection(self.connection)
        if self.connection is None or self.connection.gate.closed:
            self.connection = None
            if self._leader_fd is not None:
                os.close(self._leader_fd)
                self._leader_fd = None
        if outcome.error is not None:
            raise outcome.error

    def _open(self) -> ManagedConnection:
        if self.connection is None:
            self.connection = self.factory.open_maintenance()
        self.connection.gate.check_owner()
        if self._leader_fd is None:
            self._leader_fd = open_lock(self.factory.workspace, "checkpoint.lock")
        return self.connection

    def _token(self) -> OperationToken:
        return OperationToken(time.monotonic() + self.factory.config.db_busy_timeout_ms / 1000)

    def _publish(self, connection: ManagedConnection, state: WalState) -> None:
        # Caller owns shared or exclusive gate, never acquire a nested SH scope.
        connection.execute("BEGIN IMMEDIATE")
        try:
            self.factory.guard.check(connection)
            connection.execute("UPDATE settings SET maintenance=? WHERE singleton=1", (state.model_dump_json(),))
            connection.execute("COMMIT")
        except BaseException:
            if not connection.get_autocommit():
                connection.execute("ROLLBACK")
            raise
        self.factory.status_cache.update(state)

    def _start_attempt(self, connection: ManagedConnection) -> WalState | None:
        with connection.gate.transaction(self._token()):
            connection.execute("BEGIN IMMEDIATE")
            try:
                self.factory.guard.check(connection)
                state = WalState.read(connection)
                self.factory.status_cache.update(state)
                now = time.time()
                deadline = state.next_attempt_not_before or 0
                if deadline > now + 1:
                    state = state.model_copy(update={"next_attempt_not_before": now + 1})
                    due = False
                else:
                    due = deadline <= now
                    if due:
                        state = state.model_copy(
                            update={
                                "attempt_id": uuid4().hex,
                                "attempt_started_at": now,
                                "next_attempt_not_before": now + 1,
                            }
                        )
                if due or deadline > now + 1:
                    connection.execute(
                        "UPDATE settings SET maintenance=? WHERE singleton=1", (state.model_dump_json(),)
                    )
                connection.execute("COMMIT")
            except BaseException:
                if not connection.get_autocommit():
                    connection.execute("ROLLBACK")
                raise
            self.factory.status_cache.update(state)
            return state if due else None

    def run_once(self, trigger: Trigger) -> dict[str, object]:
        """Attempt shared PASSIVE and eligible RESTART with shared start/finish cooldown."""
        connection = self._open()
        if self._leader_fd is None:
            raise RuntimeError("maintenance owner not initialized")
        try:
            fcntl.flock(self._leader_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return self.status()
        state: WalState | None = None
        try:
            state = self._start_attempt(connection)
            if state is None:
                return self.status()
            self._attempt(connection, state, trigger)
        except (apsw.Error, BusyError, LimitError, OSError) as error:
            if state is not None:
                self._failed_attempt(connection, state)
            elif not isinstance(error, (BusyError, LimitError)):
                # No successful start publication: do not run expensive work.
                self.factory.status_cache.update(WalState(last_attempt="failed"))
        finally:
            fcntl.flock(self._leader_fd, fcntl.LOCK_UN)
        return self.status()

    def _failed_attempt(self, connection: ManagedConnection, state: WalState) -> None:
        # Native-work finish precedes its own publication fsync. Keep leader
        # ownership until publication returns; its duration can consume cooldown.
        now = time.time()
        failed = state.model_copy(
            update={
                "phase": "unknown",
                "last_attempt": "failed",
                "attempt_finished_at": now,
                "next_attempt_not_before": now + 1,
            }
        )
        self.factory.status_cache.update(failed)
        try:
            with connection.gate.transaction(self._token()):
                self._publish(connection, failed)
        except (apsw.Error, BusyError, LimitError, OSError):
            # Keep the failure snapshot; a lost publication is never success.
            self.factory.status_cache.update(failed)

    def _attempt(self, connection: ManagedConnection, state: WalState, trigger: Trigger) -> None:
        with connection.gate.transaction(self._token()):
            self.factory.guard.check(connection)
            policy = self.factory.guard.policy(connection)
            log, backfilled = connection.wal_checkpoint("main", apsw.SQLITE_CHECKPOINT_PASSIVE)
            page = connection.pragma("page_size")
            allocated = allocation(self.factory.workspace)
        valid = log >= 0 and 0 <= backfilled <= log and isinstance(page, int) and page > 0 and allocated is not None
        backlog = (log - backfilled) * page if valid else None
        pressure = (
            state.phase != "normal"
            or trigger == "pressure"
            or allocated is None
            or allocated >= policy.wal_high_bytes
            or (backlog is not None and backlog >= policy.wal_high_bytes)
        )
        reset = pressure or (valid and log * page >= policy.wal_low_bytes)
        restarted = False
        result = "passive"
        if reset:
            try:
                with connection.gate.reset_window("pressure" if pressure else "opportunistic") as window:
                    self.factory.status_cache.reset(active=True)
                    try:
                        connection.set_busy_timeout(window.busy_timeout_ms)
                        log, backfilled = connection.wal_checkpoint("main", apsw.SQLITE_CHECKPOINT_RESTART)
                        restarted = log >= 0 and 0 <= backfilled <= log
                        result = "restart" if restarted else "invalid"
                        finished = self._result(
                            state, policy, log, backfilled, page, allocated, restarted=restarted, result=result
                        )
                        self._publish(connection, finished)
                    finally:
                        self.factory.status_cache.reset(active=False)
                        connection.set_busy_timeout(self.factory.config.db_busy_timeout_ms)
            except (BusyError, apsw.BusyError):
                result = "skipped_busy"
            else:
                return
        finished = self._result(state, policy, log, backfilled, page, allocated, restarted=restarted, result=result)
        with connection.gate.transaction(self._token()):
            self._publish(connection, finished)

    @staticmethod
    def _result(
        state: WalState,
        policy: WorkspacePolicy,
        log: int,
        backfilled: int,
        page: object,
        allocated: int | None,
        *,
        restarted: bool,
        result: str,
    ) -> WalState:
        now = time.time()
        valid = log >= 0 and 0 <= backfilled <= log and isinstance(page, int) and page > 0 and allocated is not None
        page_value = page if isinstance(page, int) and page > 0 else None
        backlog = (log - backfilled) * page_value if valid and page_value is not None else None
        phase: Phase = "unknown"
        if valid and backlog is not None and allocated is not None:
            recovered = backlog <= policy.wal_low_bytes and (allocated <= policy.wal_low_bytes or restarted)
            if recovered or (
                state.phase == "normal" and allocated < policy.wal_high_bytes and backlog < policy.wal_high_bytes
            ):
                phase = "normal"
            else:
                phase = "pressure"
        return WalState(
            phase=phase,
            sample_seq=state.sample_seq + 1,
            sample_at=now,
            allocated_bytes=allocated,
            log_frames=log if valid else None,
            checkpointed_frames=backfilled if valid else None,
            page_size=page_value if valid else None,
            unbackfilled_bytes=backlog,
            pressure_started_at=None if phase == "normal" else (state.pressure_started_at or now),
            attempt_id=state.attempt_id,
            attempt_started_at=state.attempt_started_at,
            attempt_finished_at=now,
            next_attempt_not_before=now + 1,
            checkpoint_mode="RESTART" if result in ("restart", "skipped_busy") else "PASSIVE",
            last_attempt=result,
        )


def _startup_failed(ready: asyncio.Future[None], error: BaseException) -> None:
    if not ready.done():
        ready.set_exception(error)
