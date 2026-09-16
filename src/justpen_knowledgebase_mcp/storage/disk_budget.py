"""Advisory free-space checks through already validated managed descriptors."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from ..errors import StorageIOError

if TYPE_CHECKING:
    from ..config import WorkspacePolicy


def check_space(managed_dir_fds: tuple[int, ...], incoming_bytes: int, policy: WorkspacePolicy) -> None:
    """Check copy device first and DB reserve separately, once per device.

    This is neither a quota nor an atomic reservation. External/concurrent
    writers and native temporary/index files may still exhaust the device.
    """
    seen: set[int] = set()
    try:
        for index, fd in enumerate(managed_dir_fds):
            device = os.fstat(fd).st_dev
            if device in seen:
                continue
            seen.add(device)
            available = os.fstatvfs(fd)
            required = policy.disk_reserve_bytes + (incoming_bytes if index == 0 else 0)
            if available.f_bavail * available.f_frsize < required:
                raise StorageIOError("IO_ERROR: DISK_RESERVE")
    except OSError as exc:
        raise StorageIOError("IO_ERROR: disk measurement failed") from exc


CheckSpace = check_space
