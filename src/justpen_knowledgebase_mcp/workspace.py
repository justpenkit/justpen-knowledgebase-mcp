"""Pinned workspace descriptors and no-follow file operations.

Native SQLite still requires names: its VFS validates pinned identities before
handing names to the native implementation. This is not an OS sandbox against
hostile processes concurrently moving managed directories.
"""

from __future__ import annotations

import errno
import os
import stat
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Self
from uuid import uuid4

from .errors import ConfigurationError, PathDeniedError, StorageIOError
from .storage.filesystem import validate_local_directory

if TYPE_CHECKING:
    from collections.abc import Generator
    from types import TracebackType

    from .config import ServerConfig

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class WorkspacePaths:
    """Own root and managed directory descriptors until every DB is closed."""

    def __init__(self, config: ServerConfig) -> None:
        """Validate root identity and create private managed directories."""
        self.configured_root = config.workspace_dir
        self._fds: dict[Path, int] = {}
        self.root_fd = -1
        try:
            self._initialize(config)
        except OSError as exc:
            self.close()
            raise ConfigurationError("CONFIGURATION: workspace unavailable") from exc
        except (ConfigurationError, PathDeniedError):
            self.close()
            raise

    def _initialize(self, config: ServerConfig) -> None:
        self.root = self.configured_root.resolve(strict=True)
        self.root_fd = os.open(self.configured_root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        if self.identity(os.fstat(self.root_fd)) != self.identity(self.root.stat()):
            raise ConfigurationError("CONFIGURATION: ROOT_CHANGED")
        self.data = self.absolute(config.data_dir)
        self.db = self.absolute(config.db_path or self.data / "graph.sqlite3")
        self.evidence = self.absolute(config.evidence_dir or self.data / "evidence")
        self.tmp = self.absolute(config.tmp_dir or self.data / "tmp")
        self.locks = self.absolute(config.lock_dir or self.data / "locks")
        for path in (self.data, self.db.parent, self.evidence, self.tmp, self.locks):
            if path not in self._fds:
                self._fds[path] = self.open_directory(path, create=True)
                validate_local_directory(self._fds[path])
        if os.fstat(self._fds[self.tmp]).st_dev != os.fstat(self._fds[self.evidence]).st_dev:
            raise ConfigurationError("CONFIGURATION: TMP_EVIDENCE_DEVICE_MISMATCH")
        self.validate_native(self.db)
        try:
            fd = os.open(
                self.db.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._fds[self.db.parent],
            )
        except FileExistsError:
            return
        os.close(fd)

    @staticmethod
    def identity(value: os.stat_result) -> tuple[int, int]:
        """Return a filesystem identity independent of path spelling."""
        return value.st_dev, value.st_ino

    def relative(self, value: str | Path) -> Path:
        """Strip only root prefixes, without resolving child symlinks."""
        if not str(value):
            raise PathDeniedError("INVALID: empty path")
        path = Path(value)
        if ".." in path.parts:
            raise PathDeniedError("PATH_DENIED: PARENT_COMPONENT")
        if not path.is_absolute():
            return path
        for root in (self.configured_root, self.root):
            if path.is_relative_to(root):
                return path.relative_to(root)
        raise PathDeniedError("PATH_DENIED: OUTSIDE_WORKSPACE")

    def absolute(self, value: str | Path) -> Path:
        """Return a lexical canonical-root name for validated traversal."""
        return self.root / self.relative(value)

    @staticmethod
    def _directory(name: str, parent: int, *, create: bool) -> int:
        try:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(name, 0o700, dir_fd=parent)
            return os.open(name, _DIR_FLAGS, dir_fd=parent)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise PathDeniedError("PATH_DENIED: SYMLINK_COMPONENT") from exc
            raise

    def open_directory(self, value: str | Path, *, create: bool = False) -> int:
        """Traverse each component from the pinned root using openat semantics."""
        fd = os.dup(self.root_fd)
        try:
            for component in self.relative(value).parts:
                child = self._directory(component, fd, create=create)
                os.close(fd)
                fd = child
        except BaseException:
            os.close(fd)
            raise
        return fd

    def validate_native(self, value: str | Path) -> Path:
        """Check pinned directory identity and existing writable inode aliases."""
        path = self.absolute(value)
        parent = self.open_directory(path.parent)
        try:
            pinned = self._fds.get(path.parent)
            if pinned is not None and self.identity(os.fstat(parent)) != self.identity(os.fstat(pinned)):
                raise StorageIOError("IO_ERROR: MANAGED_DIRECTORY_CHANGED")
            try:
                item = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return path
            if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
                raise PathDeniedError("PATH_DENIED: UNSAFE_MANAGED_FILE")
        finally:
            os.close(parent)
        return path

    def _is_managed(self, path: Path) -> bool:
        return any(path.is_relative_to(base) for base in (self.data, self.evidence, self.tmp, self.locks)) or path in {
            self.db,
            Path(str(self.db) + "-wal"),
            Path(str(self.db) + "-shm"),
            Path(str(self.db) + "-journal"),
        }

    @contextmanager
    def open_import(self, value: str | Path) -> Generator[int]:
        """Yield a regular source descriptor, checking that its bytes stay stable."""
        path = self.absolute(value)
        if self._is_managed(path):
            raise PathDeniedError("PATH_DENIED: MANAGED_SOURCE")
        parent = self.open_directory(path.parent)
        try:
            try:
                fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise PathDeniedError("PATH_DENIED: SYMLINK_COMPONENT") from exc
                raise
        finally:
            os.close(parent)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise PathDeniedError("PATH_DENIED: UNSAFE_SOURCE")
            yield fd
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise StorageIOError("IO_ERROR: SOURCE_CHANGED")
        finally:
            os.close(fd)

    @contextmanager
    def stage(self) -> Generator[tuple[str, int]]:
        """Create a private staging file and remove it on every exit path."""
        name = uuid4().hex
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._fds[self.tmp])
        try:
            yield name, fd
        finally:
            os.close(fd)
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=self._fds[self.tmp])

    def publish(self, staged_name: str, relative_name: str) -> None:
        """Atomically publish staged bytes; callers coordinate DB recovery separately."""
        if (
            Path(staged_name).name != staged_name
            or Path(relative_name).is_absolute()
            or ".." in Path(relative_name).parts
        ):
            raise PathDeniedError("PATH_DENIED: INVALID_PUBLISH_PATH")
        path = self.evidence / relative_name
        parent = self.open_directory(path.parent, create=True)
        try:
            self.validate_native(path)
            fd = os.open(staged_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._fds[self.tmp])
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(staged_name, path.name, src_dir_fd=self._fds[self.tmp], dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(parent)

    def close(self) -> None:
        """Release owned descriptors after consumers have stopped."""
        for fd in self._fds.values():
            os.close(fd)
        self._fds.clear()
        if self.root_fd >= 0:
            os.close(self.root_fd)
            self.root_fd = -1

    def __enter__(self) -> Self:
        """Enter the managed workspace lifetime."""
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        """Close descriptors on context exit."""
        self.close()
