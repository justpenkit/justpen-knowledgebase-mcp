"""Content-addressed file I/O; callers own bucket-before-DB coordination."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import os
import re
import sys
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ..errors import BusyError, InvalidParamsError, StorageIOError
from ..evidence import INLINE_LIMIT, EvidenceReadResult
from ..responses import bounded_response
from .admission import open_lock
from .disk_budget import CheckSpace

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from ..config import WorkspacePolicy
    from ..evidence import ReadEvidenceRequest
    from ..workspace import WorkspacePaths


@dataclass(frozen=True)
class StagedEvidence:
    """Verified bytes owned exclusively by one job/token staging name."""

    name: str
    sha256: str
    byte_size: int


@dataclass(frozen=True)
class VerifiedBlob:
    """Rehashed owned bytes plus an inode identity rechecked under the publish bucket."""

    sha256: str
    byte_size: int
    identity: list[int]


def sync_evidence(fd: int) -> None:
    """Request regular fsync and macOS device-cache flush; errors propagate."""
    os.fsync(fd)
    if sys.platform == "darwin":
        fullsync = getattr(fcntl, "F_FULLFSYNC", None)
        if fullsync is None:
            raise StorageIOError("IO_ERROR: F_FULLFSYNC unavailable")
        fcntl.fcntl(fd, fullsync)


def stat_identity(item: os.stat_result) -> list[int]:
    """Stable enqueue/descriptor identity, including ctime and nanosecond mtime."""
    return [item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns]


def stage_name(job_id: str, token: str) -> str:
    """Derive a single owned component from validated job and claim identities."""
    return f"{UUID(job_id)}.{UUID(token)}.stage"


def job_bucket(job_id: str) -> str:
    """Admission/stage recovery share the bounded blob bucket namespace."""
    return hashlib.sha256(("job:" + str(UUID(job_id))).encode("ascii")).hexdigest()


def _write_all(fd: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise StorageIOError("IO_ERROR: incomplete evidence write")
        remaining = remaining[written:]


class EvidenceStore:
    """Secure synchronous bounded I/O, executed on the dedicated I/O lanes."""

    def __init__(self, workspace: WorkspacePaths, policy: WorkspacePolicy) -> None:
        """Use the shared pinned path owner and persisted workspace policy."""
        self.workspace = workspace
        self.policy = policy
        self.directory_fds = (workspace.managed_fd(workspace.tmp), workspace.managed_fd(workspace.db.parent))

    def source_stat(self, path: str) -> list[int]:
        """Validate the import source and capture identity at admission."""
        try:
            with self.workspace.open_import(path) as fd:
                return stat_identity(os.fstat(fd))
        except OSError as exc:
            raise StorageIOError("IO_ERROR: source unavailable") from exc

    @staticmethod
    def blob_name(sha256: str) -> str:
        """Managed blob names can only be constructed from a lowercase digest."""
        if re.fullmatch("[0-9a-f]{64}", sha256) is None:
            raise InvalidParamsError("invalid evidence hash")
        return f"{sha256[:2]}/{sha256[2:4]}/{sha256}"

    @contextmanager
    def bucket(self, sha256: str, *, exclusive: bool, deadline: float) -> Generator[None]:
        """Take an independent OFD flock in one of at most 4096 stable buckets."""
        fd = self.acquire_bucket(sha256, exclusive=exclusive, deadline=deadline)
        try:
            yield
        finally:
            os.close(fd)

    def acquire_bucket(self, sha256: str, *, exclusive: bool, deadline: float) -> int:
        """Acquire outside every DB transaction; the caller closes the returned OFD."""
        self.blob_name(sha256)
        fd = open_lock(self.workspace, f"evidence-{sha256[:3]}.lock")
        try:
            while True:
                try:
                    fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BusyError("evidence lock deadline exceeded") from None
                    time.sleep(min(remaining, 0.005))
                else:
                    return fd
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def _stage(self, job_id: str, token: str, size: int) -> Generator[tuple[str, int]]:
        CheckSpace(self.directory_fds, size, self.policy)
        name = stage_name(job_id, token)
        fd = self.workspace.create_managed_file(self.workspace.tmp / name)
        try:
            yield name, fd
            sync_evidence(fd)
            os.fsync(self.workspace.managed_fd(self.workspace.tmp))
        except BaseException:
            # This exclusive creation belongs to this call alone, never to another token.
            with suppress(OSError, StorageIOError):
                self.workspace.unlink_managed_file(self.workspace.tmp / name)
            raise
        finally:
            os.close(fd)

    def stage_inline(self, content: bytes, job_id: str, token: str) -> StagedEvidence:
        """Stage bounded decoded bytes durably before publishing a job reference."""
        if len(content) > INLINE_LIMIT:
            raise InvalidParamsError("inline decoded byte limit")
        try:
            with self._stage(job_id, token, len(content)) as (name, fd):
                CheckSpace(self.directory_fds, len(content), self.policy)
                _write_all(fd, content)
                return StagedEvidence(name, hashlib.sha256(content).hexdigest(), len(content))
        except OSError as exc:
            raise StorageIOError("IO_ERROR: inline staging failed") from exc

    def copy_path(
        self, path: str, expected: list[int], job_id: str, token: str, check: Callable[[], None], *, short: bool = False
    ) -> StagedEvidence:
        """Restart at byte zero; hash/copy bounded chunks and compare the same descriptor."""
        try:
            with self.workspace.open_import(path) as source:
                if stat_identity(os.fstat(source)) != expected or (short and expected[2] > INLINE_LIMIT):
                    raise StorageIOError("IO_ERROR: SOURCE_CHANGED")
                with self._stage(job_id, token, expected[2]) as (name, destination):
                    digest = hashlib.sha256()
                    copied = 0
                    while True:
                        check()
                        piece = os.read(source, min(65536, self.policy.disk_check_interval_bytes))
                        if not piece:
                            break
                        copied += len(piece)
                        if copied > expected[2] or (short and copied > INLINE_LIMIT):
                            raise StorageIOError("IO_ERROR: SOURCE_CHANGED")
                        CheckSpace(self.directory_fds, len(piece), self.policy)
                        _write_all(destination, piece)
                        digest.update(piece)
                    if copied != expected[2] or stat_identity(os.fstat(source)) != expected:
                        raise StorageIOError("IO_ERROR: SOURCE_CHANGED")
                    check()
                    return StagedEvidence(name, digest.hexdigest(), copied)
        except OSError as exc:
            raise StorageIOError("IO_ERROR: evidence copy failed") from exc

    def verify_blob(self, sha256: str, byte_size: int, check: Callable[[], None]) -> VerifiedBlob | None:
        """Rehash an owned complete blob outside locks; caller supplied hashes never enter this route."""
        path = self.workspace.evidence / self.blob_name(sha256)
        try:
            with self.workspace.open_managed_file(path) as fd:
                before = os.fstat(fd)
                digest = hashlib.sha256()
                size = 0
                while True:
                    check()
                    piece = os.read(fd, 65536)
                    if not piece:
                        break
                    digest.update(piece)
                    size += len(piece)
                if (
                    size != byte_size
                    or digest.hexdigest() != sha256
                    or stat_identity(os.fstat(fd)) != stat_identity(before)
                ):
                    raise StorageIOError("IO_ERROR: EVIDENCE_HASH_MISMATCH")
                return VerifiedBlob(sha256, size, stat_identity(before))
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StorageIOError("IO_ERROR: owned evidence verification failed") from exc

    def recheck_blob(self, verified: VerifiedBlob) -> None:
        """After taking the bucket, confirm the verified inode still occupies the canonical name."""
        with self.workspace.open_managed_file(self.workspace.evidence / self.blob_name(verified.sha256)) as fd:
            if stat_identity(os.fstat(fd)) != verified.identity:
                raise StorageIOError("IO_ERROR: EVIDENCE_CHANGED")

    def publish(self, staged: StagedEvidence) -> None:
        """Sync regular data then rename and sync every newly created hash directory."""
        try:
            with self.workspace.open_managed_file(self.workspace.tmp / staged.name) as fd:
                sync_evidence(fd)
            relative = self.blob_name(staged.sha256)
            self.workspace.publish(staged.name, relative)
            for path in (self.workspace.evidence / staged.sha256[:2], self.workspace.evidence, self.workspace.tmp):
                fd = self.workspace.open_directory(path)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except OSError as exc:
            raise StorageIOError("IO_ERROR: evidence publish failed") from exc

    def discard_stage(self, job_id: str, token: str) -> None:
        """Remove only the exact staging name owned by this immutable token."""
        self.workspace.unlink_managed_file(self.workspace.tmp / stage_name(job_id, token))

    def unlink_blob(self, sha256: str) -> None:
        """Caller holds exclusive bucket and revalidated pending intent authority."""
        self.workspace.unlink_managed_file(self.workspace.evidence / self.blob_name(sha256))

    def unlink_orphan_blob(self, sha256: str) -> None:
        """Caller holds EX bucket plus a purge-fenced proof of no canonical/peer owner."""
        self.workspace.unlink_managed_file(self.workspace.evidence / self.blob_name(sha256))

    def read_slice(self, sha256: str, size: int, encoding: str, request: ReadEvidenceRequest) -> dict[str, Any]:
        """Read an exact source-byte range after caller's shared-lock ready check."""
        if request.offset > size:
            raise InvalidParamsError("offset exceeds evidence size")
        length = min(request.length, size - request.offset)
        try:
            with self.workspace.open_managed_file(self.workspace.evidence / self.blob_name(sha256)) as fd:
                prefix = os.pread(fd, 4, 0)
                raw = os.pread(fd, length, request.offset)
                if len(raw) != length or os.fstat(fd).st_size != size:
                    raise StorageIOError("IO_ERROR: evidence size mismatch")
        except OSError as exc:
            raise StorageIOError("IO_ERROR: evidence read failed") from exc
        content = (
            base64.b64encode(raw).decode("ascii")
            if request.format == "base64"
            else _decode_range(raw, prefix, encoding, request.offset)
        )
        result = {
            "evidence_id": request.evidence_id,
            "sha256": sha256,
            "total_size": size,
            "returned_range": {"offset": request.offset, "length": length},
            "format": request.format,
            "content": content,
        }
        return bounded_response(EvidenceReadResult.model_validate(result).model_dump(mode="json"))


def _decode_range(raw: bytes, prefix: bytes, encoding: str, offset: int) -> str:
    if not raw:
        return ""
    if encoding == "auto":
        if prefix.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            raise InvalidParamsError("unsupported text encoding")
        encoding = (
            "utf-16le" if prefix.startswith(b"\xff\xfe") else "utf-16be" if prefix.startswith(b"\xfe\xff") else "utf-8"
        )
    if encoding in ("utf-16le", "utf-16be") and offset % 2:
        raise InvalidParamsError("TEXT_BOUNDARY")
    try:
        return raw.decode(encoding, errors="strict")
    except UnicodeError as exc:
        raise InvalidParamsError("TEXT_BOUNDARY") from exc
