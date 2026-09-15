"""Persistent owner-thread descriptors implementing the workspace reset barrier."""

from __future__ import annotations

import fcntl
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ..errors import WalBusyError

if TYPE_CHECKING:
    from collections.abc import Generator

    from ..workspace import WorkspacePaths
    from .worker import OperationToken


@dataclass(frozen=True)
class ResetWindow:
    """Remaining SQLite lock-wait allowance, not a native I/O deadline."""

    deadline: float
    opportunistic: bool

    @property
    def busy_timeout_ms(self) -> int:
        """Budget SQLite waiting after acquisition; native fsync may outlive it."""
        return 0 if self.opportunistic else max(0, min(200, int((self.deadline - time.monotonic()) * 1000)))


def open_lock(workspace: WorkspacePaths, name: str) -> int:
    """Open an independent lock OFD through the canonical workspace validator."""
    path = workspace.validate_native(workspace.locks / name)
    parent = workspace.open_directory(path.parent)
    try:
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            return os.open(path.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        except FileExistsError:
            workspace.validate_native(path)
            return os.open(path.name, flags, dir_fd=parent)
    finally:
        os.close(parent)


class DbAdmissionGate:
    """One non-reentrant scope per serial owner; descriptors survive operations."""

    def __init__(self, workspace: WorkspacePaths) -> None:
        """Open independent descriptors before the owner's SQLite connection."""
        self.owner = threading.get_ident()
        self.active_token: OperationToken | None = None
        self.active = False
        self.closed = False
        self.intent_fd = open_lock(workspace, "reset-intent.lock")
        try:
            self.transactions_fd = open_lock(workspace, "db-transactions.lock")
        except BaseException:
            os.close(self.intent_fd)
            raise

    def check_owner(self) -> None:
        """Reject use after close or from another worker thread."""
        if self.owner != threading.get_ident():
            raise RuntimeError("database gate used outside owner thread")
        if self.closed:
            raise RuntimeError("database gate closed")

    def _enter(self, token: OperationToken | None) -> None:
        self.check_owner()
        if self.active:
            raise RuntimeError("database gate scope already active")
        self.active = True
        self.active_token = token

    @staticmethod
    def _acquire(fd: int, mode: int, deadline: float, token: OperationToken | None = None) -> None:
        while True:
            if token is not None and token.cancelled:
                token.check()
            try:
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WalBusyError(
                        "RESET_PENDING", token.wal_retry_after_ms if token is not None else 1000
                    ) from None
                time.sleep(min(0.005, remaining))
            else:
                return

    @contextmanager
    def transaction(self, operation_token: OperationToken) -> Generator[None]:
        """Acquire intent SH then transactions SH and hold through SQL cleanup."""
        self._enter(operation_token)
        intent = transactions = False
        try:
            operation_token.check()
            self._acquire(self.intent_fd, fcntl.LOCK_SH, operation_token.deadline, operation_token)
            intent = True
            self._acquire(self.transactions_fd, fcntl.LOCK_SH, operation_token.deadline, operation_token)
            transactions = True
            fcntl.flock(self.intent_fd, fcntl.LOCK_UN)
            intent = False
            operation_token.check()
            yield
        finally:
            if transactions:
                fcntl.flock(self.transactions_fd, fcntl.LOCK_UN)
            if intent:
                fcntl.flock(self.intent_fd, fcntl.LOCK_UN)
            self.active_token = None
            self.active = False

    @contextmanager
    def reset_window(self, mode: Literal["opportunistic", "pressure"]) -> Generator[ResetWindow]:
        """Use two NB attempts for low; drain at most 500ms for pressure.

        The caller must already own checkpoint.lock and hold no SQL transaction.
        Locks remain held across native RESTART/publication, even if I/O stalls.
        """
        if mode not in ("opportunistic", "pressure"):
            raise ValueError("invalid reset mode")
        self._enter(None)
        deadline = time.monotonic() + (0 if mode == "opportunistic" else 0.5)
        intent = transactions = False
        try:
            self._acquire(self.intent_fd, fcntl.LOCK_EX, deadline)
            intent = True
            self._acquire(self.transactions_fd, fcntl.LOCK_EX, deadline)
            transactions = True
            yield ResetWindow(deadline, mode == "opportunistic")
        finally:
            if transactions:
                fcntl.flock(self.transactions_fd, fcntl.LOCK_UN)
            if intent:
                fcntl.flock(self.intent_fd, fcntl.LOCK_UN)
            self.active = False

    def close(self) -> None:
        """Close descriptors only after the owner drained and native close ended."""
        if self.closed:
            return
        self.check_owner()
        if self.active:
            raise RuntimeError("cannot close an active gate scope")
        os.close(self.transactions_fd)
        os.close(self.intent_fd)
        self.closed = True
