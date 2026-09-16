"""APSW native runtime validation and consistently configured connections."""

from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Self
from weakref import WeakValueDictionary

import apsw
from typing_extensions import override

from ..config import WorkspacePolicy
from ..errors import BusyError, ConfigurationError, LimitError, StorageIOError, WalBusyError
from .admission import DbAdmissionGate
from .maintenance import StatusCache, WalState, allocation
from .schema import SchemaGuard
from .vfs import WorkspaceVFS
from .worker import OperationToken, OwnerOutcome

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from types import TracebackType

    from ..config import ServerConfig
    from ..workspace import WorkspacePaths
    from .maintenance import Trigger


class _BootstrapMutex:
    """Weakly registered coordination for one pinned workspace's native startup."""

    def __init__(self) -> None:
        self.lock = threading.Lock()


_BOOTSTRAP_REGISTRY: WeakValueDictionary[tuple[int, int], _BootstrapMutex] = WeakValueDictionary()
_BOOTSTRAP_REGISTRY_LOCK = threading.Lock()


def _bootstrap_mutex(workspace: WorkspacePaths) -> _BootstrapMutex:
    identity = workspace.identity(os.fstat(workspace.root_fd))
    with _BOOTSTRAP_REGISTRY_LOCK:
        mutex = _BOOTSTRAP_REGISTRY.get(identity)
        if mutex is None:
            mutex = _BootstrapMutex()
            _BOOTSTRAP_REGISTRY[identity] = mutex
        return mutex


def _remaining_wait_ms(deadline: float) -> int:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LimitError("database startup deadline exceeded")
    return max(1, int(remaining * 1000))


class ManagedConnection(apsw.Connection):
    """Factory connection whose native lifetime belongs to one gated owner."""

    def __init__(self, factory: SQLiteRuntime, *, reader: bool = False) -> None:
        """Gate native open, configuration and failure cleanup as one scope."""
        self._retired = False
        with factory.bootstrap() as startup:
            self.startup_deadline = startup.deadline
            self.gate = DbAdmissionGate(factory.workspace)
            self.policy = WorkspacePolicy()
            self.close_timeout_ms = factory.config.db_busy_timeout_ms
            try:
                with self.gate.transaction(startup):
                    super().__init__(
                        str(factory.workspace.db),
                        vfs=factory.vfs.name,
                        flags=apsw.SQLITE_OPEN_READWRITE | apsw.SQLITE_OPEN_CREATE | apsw.SQLITE_OPEN_NOFOLLOW,
                    )
                    try:
                        factory.configure(self)
                        if reader:
                            self.pragma("query_only", 1)
                        startup.check()
                        self.set_busy_timeout(self.close_timeout_ms)
                    except BaseException:
                        self._finish_native_close(force=True)
                        raise
            except BaseException:
                # Native constructor failure owns its partial native resource cleanup.
                # Configuration failure was explicitly closed while still gated.
                self.gate.close()
                raise

    def close_native(self, *, force: bool = False) -> None:
        """Call APSW close only while this owner's lifetime scope is active."""
        self.gate.check_owner()
        if not self.gate.active:
            raise RuntimeError("native close requires gate scope")
        super().close(force)

    def _finish_native_close(self, *, force: bool) -> BaseException | None:
        first_error: BaseException | None = None
        while True:
            outcome = OwnerOutcome()
            with outcome:
                self.close_native(force=force)
            first_error = first_error or outcome.error
            try:
                self.get_autocommit()
            except apsw.ConnectionClosedError:
                return first_error
            # Failed or incomplete native close retains ownership. Retry with
            # force on this owner; the shutdown observer remains independent.
            force = True
            time.sleep(0.01)

    @property
    def retired(self) -> bool:
        """Whether failed transaction cleanup required confirmed native closure."""
        return self._retired

    def rollback_or_retire(self) -> BaseException | None:
        """Restore autocommit or close natively before the caller releases its gate.

        Return cleanup failure separately so callers preserve the original SQL
        or callback error. Gate descriptors belong to the enclosing scope until
        it unwinds; close() releases them after confirmed native retirement.
        """
        self.gate.check_owner()
        if not self.gate.active:
            raise RuntimeError("rollback cleanup requires gate scope")
        outcome = OwnerOutcome()
        with outcome:
            if not self.get_autocommit():
                self.execute("ROLLBACK")
            if not self.get_autocommit():
                raise StorageIOError("database rollback did not end transaction")
        if outcome.error is not None:
            self._finish_native_close(force=True)
            self._retired = True
        return outcome.error

    @override
    def close(self, force: bool = False) -> None:
        """Serialize last-close; surface original native failure after cleanup."""
        if self.gate.closed:
            return
        if self.retired:
            self.gate.close()
            return
        with self.gate.transaction(OperationToken(time.monotonic() + self.close_timeout_ms / 1000)):
            error = self._finish_native_close(force=force)
        self.gate.close()
        if error is not None:
            raise error


class SQLiteRuntime:
    """Own VFS registration; connection owners close connections before exit."""

    def __init__(self, workspace: WorkspacePaths, config: ServerConfig) -> None:
        """Check supported SQLite/platform before registering native IO."""
        if tuple(int(part) for part in apsw.sqlitelibversion().split(".")) < (3, 51, 3):
            raise ConfigurationError("CONFIGURATION: SQLite >=3.51.3 required")
        if sys.platform not in {"darwin", "linux"}:
            raise ConfigurationError("CONFIGURATION: unsupported SQLite platform")
        self.workspace = workspace
        self._bootstrap_mutex = _bootstrap_mutex(workspace)
        self.config = config
        self.vfs = WorkspaceVFS(workspace)
        self.guard = SchemaGuard(workspace)
        self.status_cache = StatusCache()
        self.wake_maintenance: Callable[[Trigger], None] | None = None

    @contextmanager
    def bootstrap(self) -> Generator[OperationToken]:
        """Bound native initialization waits before entering any DB admission gate."""
        token = OperationToken(time.monotonic() + self.config.db_busy_timeout_ms / 1000)
        if not self._bootstrap_mutex.lock.acquire(timeout=max(0, token.deadline - time.monotonic())):
            raise LimitError("database startup deadline exceeded")
        try:
            token.check()
            yield token
        finally:
            self._bootstrap_mutex.lock.release()

    def connect(self) -> ManagedConnection:
        """Open a durable WAL connection; call only in its eventual owner thread."""
        return ManagedConnection(self)

    def configure(self, connection: ManagedConnection) -> None:
        """Configure inside the factory-owned open scope; no independent SQL owner."""
        connection.enable_load_extension(enable=False)
        connection.set_busy_timeout(_remaining_wait_ms(connection.startup_deadline))
        if self._enable_wal(connection) != "wal":
            raise ConfigurationError("CONFIGURATION: WAL unavailable")
        connection.pragma("foreign_keys", 1)
        connection.pragma("synchronous", "full")
        connection.pragma("temp_store", "file")
        if sys.platform == "darwin":
            connection.pragma("fullfsync", 1)
            connection.pragma("checkpoint_fullfsync", 1)
        if connection.execute("select json_valid('{}'), sqlite_compileoption_used('ENABLE_FTS5')").get != (1, 1):
            raise ConfigurationError("CONFIGURATION: JSON and FTS5 required")
        connection.set_busy_timeout(_remaining_wait_ms(connection.startup_deadline))
        self.guard.initialize(connection)
        policy = self.guard.policy(connection)
        connection.policy = policy
        connection.pragma("journal_size_limit", policy.journal_size_limit)
        connection.pragma("wal_autocheckpoint", policy.wal_autocheckpoint)
        self.status_cache.update(WalState.read(connection))

    def check_product(self, connection: apsw.Connection) -> None:
        """Enforce the latest shared sample in the product's transaction snapshot."""
        self.status_cache.check_health()
        policy = self.guard.policy(connection)
        state = WalState.read(connection)
        self.status_cache.update(state)
        now = time.time()
        allocated = allocation(self.workspace)
        physical_high = allocated is None or allocated >= policy.wal_high_bytes
        if physical_high:
            self.status_cache.assessment()
        valid_normal = state.phase == "normal" and state.valid_sample(now)
        stale = valid_normal and state.sample_at is not None and now - state.sample_at > 35
        if (physical_high or not valid_normal or stale) and self.wake_maintenance is not None:
            self.wake_maintenance("stale" if stale and not physical_high else "pressure")
        if physical_high or not valid_normal:
            raise WalBusyError("WAL_PRESSURE", state.retry_after_ms(now))

    def committed(self, connection: ManagedConnection) -> None:
        """Wake after commit without replacing SQLite's autocheckpoint hook."""
        if self.wake_maintenance is not None:
            # Physical low is only a wake hint; live frames govern reset eligibility.
            size = allocation(self.workspace)
            if size is None or size >= connection.policy.wal_low_bytes:
                self.wake_maintenance("low")

    def _enable_wal(self, connection: ManagedConnection) -> object:
        # Concurrent journal-mode upgrades can return BUSY without invoking the
        # busy handler (shared-to-exclusive lock conflict). Retry the completed
        # statement within one startup budget; do not restart the full timeout.
        deadline = connection.startup_deadline
        while True:
            try:
                return connection.pragma("journal_mode", "wal")
            except apsw.BusyError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                connection.set_busy_timeout(max(1, int(remaining * 1000)))
                time.sleep(min(0.005, remaining))

    def open_writer(self) -> ManagedConnection:
        """Open a writer on its owner thread."""
        return self.connect()

    def open_reader(self) -> ManagedConnection:
        """Open and configure query-only inside the same factory admission scope."""
        return ManagedConnection(self, reader=True)

    def open_maintenance(self) -> ManagedConnection:
        """Open the dedicated maintenance owner's configured connection."""
        return self.connect()

    @staticmethod
    def close_connection(connection: ManagedConnection) -> None:
        """Drain shutdown through bounded gate retries, retaining all resources."""
        while True:
            try:
                connection.close()
            except (BusyError, LimitError):
                time.sleep(0.01)
            else:
                return

    def close(self) -> None:
        """Unregister the VFS after every connection has been closed."""
        self.vfs.unregister()

    def __enter__(self) -> Self:
        """Enter the runtime registration lifetime."""
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        """Release VFS registration on context exit."""
        self.close()


# One factory implementation retains the native workspace/VFS boundary.
ConnectionFactory = SQLiteRuntime
