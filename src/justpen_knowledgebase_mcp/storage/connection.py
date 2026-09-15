"""APSW native runtime validation and consistently configured connections."""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING, Self

import apsw

from ..errors import ConfigurationError
from .schema import SchemaGuard
from .vfs import WorkspaceVFS

if TYPE_CHECKING:
    from types import TracebackType

    from ..config import ServerConfig
    from ..workspace import WorkspacePaths


class SQLiteRuntime:
    """Own VFS registration; connection owners close connections before exit."""

    def __init__(self, workspace: WorkspacePaths, config: ServerConfig) -> None:
        """Check supported SQLite/platform before registering native IO."""
        if tuple(int(part) for part in apsw.sqlitelibversion().split(".")) < (3, 51, 3):
            raise ConfigurationError("CONFIGURATION: SQLite >=3.51.3 required")
        if sys.platform not in {"darwin", "linux"}:
            raise ConfigurationError("CONFIGURATION: unsupported SQLite platform")
        self.workspace = workspace
        self.config = config
        self.vfs = WorkspaceVFS(workspace)
        self.guard = SchemaGuard(workspace)

    def connect(self) -> apsw.Connection:
        """Open a durable WAL connection; call only in its eventual owner thread."""
        connection = apsw.Connection(
            str(self.workspace.db),
            vfs=self.vfs.name,
            flags=apsw.SQLITE_OPEN_READWRITE | apsw.SQLITE_OPEN_CREATE | apsw.SQLITE_OPEN_NOFOLLOW,
        )
        try:
            self._configure(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    def _configure(self, connection: apsw.Connection) -> None:
        connection.enable_load_extension(enable=False)
        connection.set_busy_timeout(self.config.db_busy_timeout_ms)
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
        self.guard.initialize(connection)

    def _enable_wal(self, connection: apsw.Connection) -> object:
        # Concurrent journal-mode upgrades can return BUSY without invoking the
        # busy handler (shared-to-exclusive lock conflict). Retry the completed
        # statement within one startup budget; do not restart the full timeout.
        deadline = time.monotonic() + self.config.db_busy_timeout_ms / 1000
        while True:
            try:
                return connection.pragma("journal_mode", "wal")
            except apsw.BusyError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                connection.set_busy_timeout(max(1, int(remaining * 1000)))
                time.sleep(min(0.005, remaining))

    def open_writer(self) -> apsw.Connection:
        """Open a writer on its owner thread."""
        return self.connect()

    def open_reader(self) -> apsw.Connection:
        """Open a query-only reader on its owner thread."""
        connection = self.connect()
        connection.pragma("query_only", 1)
        return connection

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
