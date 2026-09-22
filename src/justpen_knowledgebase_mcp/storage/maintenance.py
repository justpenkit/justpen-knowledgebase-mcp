"""Workspace-wide checkpoint attempts and process-local bounded status snapshots."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import math
import os
import stat
import threading
import time
from contextlib import ExitStack
from typing import TYPE_CHECKING, Literal
from uuid import uuid4
from weakref import WeakKeyDictionary

import apsw
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..errors import (
    BusyError,
    ConfigurationError,
    ContractMismatchError,
    InternalError,
    LimitError,
    McpError,
    PathDeniedError,
    StorageIOError,
    UnsupportedLayoutError,
)
from .admission import open_lock
from .worker import OperationToken, OwnerOutcome

if TYPE_CHECKING:
    from pathlib import Path

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


# `validate_native` re-derives and re-validates the name on every call: about 539
# Python calls of pathlib work around roughly seven syscalls, measured at 123.8 us
# against 3.5 us for the bare stat. Every product transaction pays it once and
# every write commit a second time. The validated name is therefore reused for at
# most this long, which is exactly how long a managed directory replaced under a
# running server can go unnoticed: the full check runs again within a second and
# `check_product` degrades to WAL_PRESSURE from then on, as it does today. The
# name's own two inode rules are not deferred with it; `allocation` re-checks
# them against the pinned descriptor on every call.
_REVALIDATE_SECONDS = 1.0

_VALIDATED_WAL: WeakKeyDictionary[WorkspacePaths, tuple[str, Path, float]] = WeakKeyDictionary()
_VALIDATED_WAL_LOCK = threading.Lock()


def _wal_name(workspace: WorkspacePaths) -> tuple[str, Path]:
    """Revalidate the WAL name at most once per `_REVALIDATE_SECONDS`."""
    now = time.monotonic()
    with _VALIDATED_WAL_LOCK:
        entry = _VALIDATED_WAL.get(workspace)
        if entry is not None and now - entry[2] < _REVALIDATE_SECONDS:
            return entry[0], entry[1]
    try:
        path = workspace.validate_native(str(workspace.db) + "-wal")
    except BaseException:
        # A directory that stopped validating must not keep serving its old name.
        with _VALIDATED_WAL_LOCK:
            _VALIDATED_WAL.pop(workspace, None)
        raise
    with _VALIDATED_WAL_LOCK:
        _VALIDATED_WAL[workspace] = (path.name, path.parent, now)
    return path.name, path.parent


def allocation(workspace: WorkspacePaths) -> int | None:
    """Measure WAL allocation; absent WAL is zero, failed measurement unknown."""
    try:
        # Reusing the validation must not reuse a resolved path: a name resolved
        # again from the root follows a symlink planted since and measures a file
        # outside the workspace. The pinned parent descriptor reads the inode the
        # open database holds, and `KeyError` stands for a workspace already
        # closed, the same unknown an uncached measurement returns then.
        name, directory = _wal_name(workspace)
        item = os.stat(name, dir_fd=workspace.managed_fd(directory), follow_symlinks=False)
    except FileNotFoundError:
        return 0
    except (KeyError, OSError, PathDeniedError, StorageIOError):
        return None
    # `validate_native`'s own two inode rules, kept on every call rather than
    # deferred with the name; only its directory-identity check waits for the
    # next full revalidation. An unsafe alias is an unknown, as `PATH_DENIED` is.
    if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
        return None
    return item.st_size


class StatusCache:
    """Atomic process-local snapshot; status never acquires SQL or file locks."""

    def __init__(self) -> None:
        """Start unavailable until a shared snapshot is observed."""
        self._lock = threading.Lock()
        self._state = WalState()
        self._cached_at: float | None = None
        self._reset = False
        self._evaluation_requested = False
        self._assessment_seq: int | None = None
        self._failure: tuple[McpError, bool, int, float] | None = None

    def update(self, state: WalState) -> None:
        """Copy the immutable snapshot after shared reads or committed publication."""
        with self._lock:
            if (state.sample_seq, state.attempt_started_at or 0, state.attempt_finished_at or 0) < (
                self._state.sample_seq,
                self._state.attempt_started_at or 0,
                self._state.attempt_finished_at or 0,
            ):
                return
            self._state = state
            self._cached_at = time.time()
            if state.phase == "normal" and state.valid_sample(self._cached_at):
                self._evaluation_requested = False
                if self._assessment_seq is not None and state.sample_seq > self._assessment_seq:
                    self._assessment_seq = None
            if (
                self._failure is not None
                and not self._failure[1]
                and state.sample_seq > self._failure[2]
                and state.phase in ("normal", "pressure")
                and state.valid_sample(self._cached_at)
                and state.sample_at is not None
                and state.sample_at >= self._failure[3]
            ):
                self._failure = None

    def assessment(self) -> None:
        """Keep a local high-allocation observation until a newer normal sample."""
        with self._lock:
            self._assessment_seq = self._state.sample_seq

    def failed(self, error: McpError, *, permanent: bool) -> None:
        """Record owner health independently of the last committed measurement."""
        with self._lock:
            if self._failure is None or not self._failure[1]:
                self._failure = (error, permanent, self._state.sample_seq, time.time())

    def check_health(self) -> None:
        """Reject product work after a permanent local maintenance fault."""
        with self._lock:
            error = self._failure[0] if self._failure is not None and self._failure[1] else None
        # Authored errors are rebuilt from their own field, not from their text:
        # their constructor takes the selector, so `type(error)(str(error))` would
        # fail on it. Every other public error carries its message as its argument.
        if isinstance(error, UnsupportedLayoutError):
            raise UnsupportedLayoutError(error.layout) from None
        if isinstance(error, ContractMismatchError):
            raise ContractMismatchError(error.dimension) from None
        if error is not None:
            raise type(error)(str(error)) from None

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
            assessment, failure = self._assessment_seq, self._failure
        valid = state.valid_sample(now)
        age = now - state.sample_at if valid and state.sample_at is not None else None
        stale = state.phase == "normal" and age is not None and age > 35
        phase = state.phase if valid or state.phase != "normal" else "unknown"
        result: dict[str, object] = state.model_dump()
        result.update(
            phase="reset"
            if reset
            else "unknown"
            if failure
            else "assessment_pending"
            if assessment is not None
            else phase,
            sample_age=age,
            cache_age=None if cached is None else max(0, now - cached),
            stale_normal=stale,
            unavailable=not valid,
            evaluation_requested=requested or stale,
            pressure_elapsed=None if state.pressure_started_at is None else max(0, now - state.pressure_started_at),
            retry_after_ms=state.retry_after_ms(now),
            estimated_completion_ms=None,
            maintenance_error=None if failure is None else failure[0].error_type,
            maintenance_failed_permanently=failure is not None and failure[1],
        )
        if failure is not None:
            result.update(last_attempt="failed", attempt_finished_at=failure[3], evaluation_requested=True)
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
        self._retry_not_before = 0.0
        self._initialized = False
        self._cycle_completed = False
        self._failure_category: str | None = None
        self._failure_logged_at = 0.0
        self._suppressed_failures = 0
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
        result = self.factory.status_cache.snapshot()
        result["maintenance_alive"] = self._thread is not None and self._thread.is_alive()
        return result

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
                        if self._stop.wait(max(0, self._retry_not_before - time.monotonic())):
                            break
                        try:
                            self.run_once(trigger)
                        except McpError:
                            # run_once already recorded a permanent, sanitized fault.
                            break
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
        try:
            connection.execute("BEGIN IMMEDIATE")
            self.factory.guard.check(connection)
            connection.execute("UPDATE settings SET maintenance=? WHERE singleton=1", (state.model_dump_json(),))
            connection.execute("COMMIT")
        except BaseException:
            connection.rollback_or_retire()
            raise
        self.factory.status_cache.update(state)

    def _start_attempt(self, connection: ManagedConnection) -> WalState | None:
        with connection.gate.transaction(self._token()):
            try:
                connection.execute("BEGIN IMMEDIATE")
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
                connection.rollback_or_retire()
                raise
            self.factory.status_cache.update(state)
            return state if due else None

    def run_once(self, trigger: Trigger) -> dict[str, object]:
        """Attempt shared PASSIVE and eligible RESTART with shared start/finish cooldown."""
        outcome = OwnerOutcome()
        result: dict[str, object] = {}
        previous_seq = self.factory.status_cache.snapshot()["sample_seq"]
        self._cycle_completed = False
        with outcome:
            result = self._run_once(trigger)
        if outcome.error is None:
            self._initialized = True
            snapshot = self.factory.status_cache.snapshot()
            if (
                self._failure_category is not None
                and self._cycle_completed
                and snapshot["maintenance_error"] is None
                and snapshot["phase"] in ("normal", "pressure")
                and snapshot["unavailable"] is False
                and isinstance(previous_seq, int)
                and isinstance(snapshot["sample_seq"], int)
                and snapshot["sample_seq"] > previous_seq
            ):
                logging.getLogger(__name__).info(
                    "maintenance_recovered: %s suppressed=%s", self._failure_category, self._suppressed_failures
                )
                self._failure_category = None
                self._suppressed_failures = 0
            return result
        if not isinstance(outcome.error, Exception):
            raise outcome.error
        public, permanent = _maintenance_failure(outcome.error)
        self._record_failure(public, permanent=permanent)
        if permanent or not self._initialized:
            raise public from None
        return self.status()

    def _record_failure(self, error: McpError, *, permanent: bool) -> None:
        self.factory.status_cache.failed(error, permanent=permanent)
        now = time.monotonic()
        self._retry_not_before = now + 1
        if error.error_type != self._failure_category or now - self._failure_logged_at >= 30:
            logging.getLogger(__name__).error(
                "maintenance_failed: %s suppressed=%s", error.error_type, self._suppressed_failures
            )
            self._failure_category = error.error_type
            self._failure_logged_at = now
        else:
            self._suppressed_failures = min(65535, self._suppressed_failures + 1)

    def _run_once(self, trigger: Trigger) -> dict[str, object]:
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
            self._cycle_completed = True
        except (apsw.Error, BusyError, LimitError, OSError) as error:
            if isinstance(error, (apsw.CorruptError, apsw.NotADBError)):
                raise
            if state is not None:
                self._failed_attempt(connection, state)
            if not isinstance(error, (BusyError, LimitError)):
                self._record_failure(StorageIOError("maintenance unavailable"), permanent=False)
        finally:
            if connection.retired:
                connection.close()
                self.connection = None
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
        if connection.retired:
            return
        try:
            with connection.gate.transaction(self._token()):
                self._publish(connection, failed)
        except (apsw.Error, BusyError, LimitError, OSError) as error:
            if isinstance(error, (apsw.CorruptError, apsw.NotADBError)):
                raise
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
        mode: Literal["PASSIVE", "RESTART"] = "PASSIVE"
        result = "passive"
        if reset:
            with ExitStack() as scopes:
                try:
                    window = scopes.enter_context(
                        connection.gate.reset_window("pressure" if pressure else "opportunistic")
                    )
                except BusyError:
                    result = "skipped_busy"
                else:
                    self.factory.status_cache.reset(active=True)
                    try:
                        connection.set_busy_timeout(window.busy_timeout_ms)
                        mode = "RESTART"
                        try:
                            log, backfilled = connection.wal_checkpoint("main", apsw.SQLITE_CHECKPOINT_RESTART)
                        except apsw.BusyError:
                            result = "skipped_busy"
                        else:
                            restarted = log >= 0 and 0 <= backfilled <= log
                            result = "restart" if restarted else "invalid"
                            finished = self._result(
                                state,
                                policy,
                                log,
                                backfilled,
                                page,
                                allocated,
                                restarted=restarted,
                                result=result,
                                checkpoint_mode=mode,
                            )
                            # Publication errors must escape; successful reuse
                            # cannot be republished after losing exclusive scope.
                            self._publish(connection, finished)
                            return
                    finally:
                        self.factory.status_cache.reset(active=False)
                        if not connection.retired:
                            connection.set_busy_timeout(self.factory.config.db_busy_timeout_ms)
        finished = self._result(
            state,
            policy,
            log,
            backfilled,
            page,
            allocated,
            restarted=False,
            result=result,
            checkpoint_mode=mode,
        )
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
        checkpoint_mode: Literal["PASSIVE", "RESTART"] = "PASSIVE",
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
            checkpoint_mode=checkpoint_mode,
            last_attempt=result,
        )


def _startup_failed(ready: asyncio.Future[None], error: BaseException) -> None:
    if not ready.done():
        ready.set_exception(error)


def _maintenance_failure(error: Exception) -> tuple[McpError, bool]:
    # Both are authored from a fixed selector, never from stored or exception text,
    # so they can name what differs where a bare `ConfigurationError` cannot: its
    # message is whatever the raising site passed and may carry workspace content.
    if isinstance(error, (UnsupportedLayoutError, ContractMismatchError)):
        return error, True
    if isinstance(error, ConfigurationError):
        return ConfigurationError("maintenance unavailable"), True
    if isinstance(error, PathDeniedError):
        return PathDeniedError("maintenance unavailable"), True
    if isinstance(error, (apsw.CorruptError, apsw.NotADBError)) or (
        isinstance(error, StorageIOError) and str(error) == "IO_ERROR: MANAGED_DIRECTORY_CHANGED"
    ):
        return StorageIOError("maintenance unavailable"), True
    if isinstance(error, (apsw.Error, BusyError, LimitError, StorageIOError, OSError)):
        return StorageIOError("maintenance unavailable"), False
    return InternalError("maintenance unavailable"), True
