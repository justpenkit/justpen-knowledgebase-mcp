"""Real managed evidence copy, publication, locking, and byte ranges."""

import base64
import hashlib
import os
import threading
import time
from uuid import uuid4

import pytest

from justpen_knowledgebase_mcp.config import ServerConfig, WorkspacePolicy
from justpen_knowledgebase_mcp.errors import BusyError, InvalidParamsError, StorageIOError
from justpen_knowledgebase_mcp.evidence import ReadEvidenceRequest
from justpen_knowledgebase_mcp.storage import evidence
from justpen_knowledgebase_mcp.storage.job_recovery import StageScan
from justpen_knowledgebase_mcp.workspace import WorkspacePaths

pytestmark = pytest.mark.integration


@pytest.fixture
def store(tmp_path):
    with WorkspacePaths(ServerConfig(workspace_dir=tmp_path)) as workspace:
        yield evidence.EvidenceStore(workspace, WorkspacePolicy())


def test_copy_hash_publish_and_source_stat(store, tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"\x00\xff" * 200000)
    expected = store.source_stat("source.bin")
    copied = store.copy_path("source.bin", expected, str(uuid4()), str(uuid4()), lambda: None)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert copied.sha256 == digest
    assert copied.byte_size == 400000
    with store.bucket(digest, exclusive=True, deadline=time.monotonic() + 1):
        store.publish(copied)
    assert (store.workspace.evidence / digest[:2] / digest[2:4] / digest).read_bytes() == source.read_bytes()
    assert source.stat().st_size == 400000
    assert not (store.workspace.tmp / copied.name).exists()
    assert (store.workspace.evidence / digest[:2] / digest[2:4] / digest).stat().st_mode & 0o777 == 0o600


def test_changed_small_source_is_rejected_before_copy(store, tmp_path):
    (tmp_path / "small").write_bytes(b"a")
    expected = store.source_stat("small")
    (tmp_path / "small").write_bytes(b"b" * 300000)
    with pytest.raises(StorageIOError, match="SOURCE_CHANGED"):
        store.copy_path("small", expected, str(uuid4()), str(uuid4()), lambda: None, short=True)
    assert list(store.workspace.tmp.iterdir()) == []


def test_source_changes_during_copy_never_publish(store, tmp_path):
    path = tmp_path / "small"
    path.write_bytes(b"a" * 1000)
    expected = store.source_stat("small")
    count = 0

    def mutate():
        nonlocal count
        count += 1
        if count == 2:
            path.write_bytes(b"b" * 1000)

    with pytest.raises(StorageIOError, match="SOURCE_CHANGED"):
        store.copy_path("small", expected, str(uuid4()), str(uuid4()), mutate, short=True)
    assert list(store.workspace.tmp.iterdir()) == []


def test_independent_same_process_descriptors_exclude_publish_and_wait_for_read(store):
    digest = "a" * 64
    failures = []
    with store.bucket(digest, exclusive=False, deadline=time.monotonic() + 1):
        with store.bucket(digest, exclusive=False, deadline=time.monotonic() + 1):
            pass

        def writer():
            try:
                with store.bucket(digest, exclusive=True, deadline=time.monotonic() + 0.02):
                    failures.append("unexpected acquisition")
            except BusyError:
                failures.append("bounded wait")

        thread = threading.Thread(target=writer)
        thread.start()
        thread.join()
    assert failures == ["bounded wait"]
    locks = list(store.workspace.locks.glob("evidence-*.lock"))
    assert len(locks) == 1
    inode = locks[0].stat().st_ino
    with store.bucket("aaa" + "b" * 61, exclusive=True, deadline=time.monotonic() + 1):
        pass
    assert locks[0].stat().st_ino == inode


@pytest.mark.parametrize(
    ("codec", "text", "offset", "length"),
    [("utf-8", "aéz", 2, 1), ("utf-16le", "a😀z", 1, 2), ("utf-16le", "a😀z", 2, 2), ("utf-16be", "a😀z", 4, 2)],
)
def test_text_boundaries_and_exact_base64(store, codec, text, offset, length):
    raw = text.encode(codec)
    staged = store.stage_inline(raw, str(uuid4()), str(uuid4()))
    store.publish(staged)
    request = ReadEvidenceRequest(evidence_id="e_" + staged.sha256, offset=offset, length=length)
    with pytest.raises(InvalidParamsError, match="TEXT_BOUNDARY"):
        store.read_slice(staged.sha256, len(raw), codec, request)
    request.format = "base64"
    result = store.read_slice(staged.sha256, len(raw), codec, request)
    assert base64.b64decode(result["content"]) == raw[offset : offset + length]
    assert result["returned_range"] == {"offset": offset, "length": length}


def test_eof_range_and_hash_metadata(store):
    staged = store.stage_inline(b"abc", str(uuid4()), str(uuid4()))
    store.publish(staged)
    request = ReadEvidenceRequest(evidence_id="e_" + staged.sha256, offset=3)
    result = store.read_slice(staged.sha256, 3, "auto", request)
    assert result["content"] == ""
    assert result["sha256"] == staged.sha256
    assert result["total_size"] == 3
    request.offset = 4
    with pytest.raises(InvalidParamsError):
        store.read_slice(staged.sha256, 3, "auto", request)


def test_regular_fsync_and_platform_fullsync_failure_is_not_success(store, monkeypatch):
    calls = []
    real = os.fsync

    def fsync(fd):
        calls.append(fd)
        real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    staged = store.stage_inline(b"durable", str(uuid4()), str(uuid4()))
    store.publish(staged)
    assert len(calls) >= 3

    def fail(fd):
        raise OSError("injected")

    monkeypatch.setattr(evidence, "sync_evidence", fail)
    with pytest.raises(StorageIOError):
        store.stage_inline(b"failure", str(uuid4()), str(uuid4()))


def test_verified_owned_blob_reuse_hashes_content_and_rejects_corruption(store):
    staged = store.stage_inline(b"owned", str(uuid4()), str(uuid4()))
    store.publish(staged)
    verified = store.verify_blob(staged.sha256, 5, lambda: None)
    assert verified.sha256 == staged.sha256
    assert verified.byte_size == 5
    path = store.workspace.evidence / store.blob_name(staged.sha256)
    path.write_bytes(b"wrong")
    with pytest.raises(StorageIOError, match="EVIDENCE_HASH_MISMATCH"):
        store.verify_blob(staged.sha256, 5, lambda: None)


@pytest.mark.parametrize("failure", ["missing", "unsupported", "io"])
def test_macos_fullsync_is_required_after_regular_sync(store, monkeypatch, failure):
    calls = []
    monkeypatch.setattr(evidence.sys, "platform", "darwin")
    monkeypatch.setattr(evidence.os, "fsync", lambda fd: calls.append("fsync"))
    if failure == "missing":
        monkeypatch.delattr(evidence.fcntl, "F_FULLFSYNC", raising=False)
    else:
        monkeypatch.setattr(evidence.fcntl, "F_FULLFSYNC", 51, raising=False)

        def fail(fd, command):
            calls.append(command)
            raise OSError(45 if failure == "unsupported" else 5, "controlled fullsync failure")

        monkeypatch.setattr(evidence.fcntl, "fcntl", fail)
    with pytest.raises(StorageIOError):
        store.stage_inline(b"must not accept", str(uuid4()), str(uuid4()))
    assert calls[0] == "fsync"
    assert list(store.workspace.tmp.iterdir()) == []


def test_reuse_revalidates_name_inode_after_unlocked_hash(store):
    staged = store.stage_inline(b"owned", str(uuid4()), str(uuid4()))
    store.publish(staged)
    verified = store.verify_blob(staged.sha256, 5, lambda: None)
    assert verified is not None
    path = store.workspace.evidence / store.blob_name(staged.sha256)
    path.unlink()
    path.write_bytes(b"owned")
    with (
        store.bucket(staged.sha256, exclusive=True, deadline=time.monotonic() + 1),
        pytest.raises(StorageIOError, match="EVIDENCE_CHANGED"),
    ):
        store.recheck_blob(verified)


def test_stage_scan_bounds_enumeration_and_progresses_past_unrecognized_entries(store):
    names = [evidence.stage_name(str(uuid4()), str(uuid4())) for _ in range(70)]
    for index, name in enumerate(names):
        (store.workspace.tmp / name).write_bytes(b"owned")
        (store.workspace.tmp / f"unrecognized-{index}").write_bytes(b"leave")
    scan = StageScan(store.workspace)
    found = []
    try:
        for _ in range(5):
            batch = scan.batch()
            assert len(batch) <= 32
            found.extend(batch)
        assert set(found) == set(names)
        assert len(found) == len(names)
        assert scan.iterator is None
        assert len(list(store.workspace.tmp.iterdir())) == 140
    finally:
        scan.close()
