"""Minimal validating wrapper inheriting the platform's locks and shared memory."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import apsw
from typing_extensions import override

from ..errors import PathDeniedError, StorageIOError

if TYPE_CHECKING:
    from ..workspace import WorkspacePaths


class WorkspaceVFS(apsw.VFS):
    """Constrain SQLite names and force unnamed native spill files into TMP_DIR."""

    def __init__(self, workspace: WorkspacePaths) -> None:
        """Register a unique wrapper over the active default platform VFS."""
        self.workspace = workspace
        self.name = "kb-" + uuid4().hex
        self.temp_open_count = 0
        super().__init__(self.name, "")

    def _allowed(self, name: str) -> str:
        try:
            return self._validate(name)
        except (OSError, PathDeniedError, StorageIOError) as exc:
            raise apsw.IOError("IO_ERROR: SQLite path unavailable") from exc

    def _validate(self, name: str) -> str:
        path = self.workspace.absolute(name)
        db = self.workspace.db
        if (
            path not in {db, Path(str(db) + "-wal"), Path(str(db) + "-shm"), Path(str(db) + "-journal")}
            and path.parent != self.workspace.tmp
        ):
            raise PathDeniedError("PATH_DENIED: SQLITE_PATH")
        return str(self.workspace.validate_native(path))

    @override
    def xFullPathname(self, name: str) -> str:
        """Do not let SQLite resolve child aliases before containment checks."""
        return self._allowed(name)

    @override
    def xOpen(self, name: str | apsw.URIFilename | None, flags: list[int]) -> apsw.VFSFile:
        """Delegate native file IO including WAL/SHM, never temp-path fallback."""
        if name is None:
            name = str(self.workspace.tmp / ("sqlite-" + uuid4().hex))
            self.temp_open_count += 1
        filename = name.filename() if isinstance(name, apsw.URIFilename) else name
        self._allowed(filename)
        if Path(filename) == self.workspace.db:
            for suffix in ("-wal", "-shm", "-journal"):
                self._allowed(filename + suffix)
        flags[0] |= apsw.SQLITE_OPEN_NOFOLLOW
        try:
            return apsw.VFSFile("", name, flags)
        except apsw.Error as exc:
            raise apsw.IOError("IO_ERROR: native SQLite open failed") from exc

    @override
    def xDelete(self, filename: str, syncdir: bool) -> None:
        """Validate native deletion paths before delegating directory sync."""
        super().xDelete(self._allowed(filename), syncdir)

    @override
    def xAccess(self, pathname: str, flags: int) -> bool:
        """Apply the same boundary to native existence and permission probes."""
        return super().xAccess(self._allowed(pathname), flags)
