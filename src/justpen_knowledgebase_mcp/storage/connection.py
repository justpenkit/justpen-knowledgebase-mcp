"""APSW native runtime validation and consistently configured connections."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Self

import apsw

from ..errors import ConfigurationError
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
        if connection.pragma("journal_mode", "wal") != "wal":
            raise ConfigurationError("CONFIGURATION: WAL unavailable")
        connection.pragma("foreign_keys", 1)
        connection.pragma("synchronous", "full")
        connection.pragma("temp_store", "file")
        if sys.platform == "darwin":
            connection.pragma("fullfsync", 1)
            connection.pragma("checkpoint_fullfsync", 1)
        if connection.execute("select json_valid('{}'), sqlite_compileoption_used('ENABLE_FTS5')").get != (1, 1):
            raise ConfigurationError("CONFIGURATION: JSON and FTS5 required")
        self._check_paths(connection)

    def _check_paths(self, connection: apsw.Connection) -> None:
        # Task 2 migrates this foundation contract into canonical schema settings.
        paths = json.dumps(
            {
                name: str(self.workspace.relative(path))
                for name, path in (
                    ("data", self.workspace.data),
                    ("db", self.workspace.db),
                    ("evidence", self.workspace.evidence),
                    ("tmp", self.workspace.tmp),
                    ("locks", self.workspace.locks),
                )
            },
            sort_keys=True,
        )
        with connection:
            connection.execute(
                "create table if not exists runtime_paths (singleton integer primary key check(singleton=1), paths text not null)"
            )
            connection.execute("insert or ignore into runtime_paths values (1, ?)", (paths,))
            if connection.execute("select paths from runtime_paths where singleton=1").get != paths:
                raise ConfigurationError("CONFIGURATION: managed paths differ from database initialization")

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
