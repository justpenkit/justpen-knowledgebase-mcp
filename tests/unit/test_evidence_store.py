"""Evidence copy/publication decisions with descriptor and filesystem operations isolated."""

import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from justpen_knowledgebase_mcp.config import WorkspacePolicy
from justpen_knowledgebase_mcp.errors import BusyError, InvalidParamsError, StorageIOError
from justpen_knowledgebase_mcp.evidence import ReadEvidenceRequest
from justpen_knowledgebase_mcp.storage import evidence, evidence_records

from .helpers import EVIDENCE, NODE, OTHER, claim, database, job, owner


@pytest.fixture
def store(monkeypatch):
    workspace = Mock(tmp=Path("/workspace/tmp"), evidence=Path("/workspace/evidence"), db=Path("/workspace/db/kb.db"))
    workspace.managed_fd.return_value = 8
    workspace.create_managed_file.return_value = 10
    workspace.open_import.side_effect = lambda _path: nullcontext(9)
    workspace.open_managed_file.side_effect = lambda _path: nullcontext(9)
    workspace.open_directory.return_value = 11
    monkeypatch.setattr(evidence, "CheckSpace", Mock())
    monkeypatch.setattr(evidence, "sync_evidence", Mock())
    monkeypatch.setattr(evidence.os, "fsync", Mock())
    monkeypatch.setattr(evidence.os, "close", Mock())
    monkeypatch.setattr(evidence.os, "write", Mock(side_effect=lambda _fd, data: len(data)))
    monkeypatch.setattr(
        evidence.os,
        "fstat",
        Mock(return_value=SimpleNamespace(st_dev=1, st_ino=2, st_size=3, st_mtime_ns=4, st_ctime_ns=5)),
    )
    return evidence.EvidenceStore(workspace, WorkspacePolicy())


def test_inline_staging_hash_and_sync_before_close(store, monkeypatch):
    result = store.stage_inline(b"abc", NODE, OTHER)
    assert result == evidence.StagedEvidence(evidence.stage_name(NODE, OTHER), hashlib.sha256(b"abc").hexdigest(), 3)
    assert isinstance(evidence.sync_evidence, Mock)
    evidence.sync_evidence.assert_called_once_with(10)
    assert isinstance(evidence.os.close, Mock)
    evidence.os.close.assert_called_once_with(10)
    assert store.source_stat("input") == [1, 2, 3, 4, 5]
    monkeypatch.setattr(evidence.os, "write", Mock(return_value=0))
    with pytest.raises(StorageIOError, match="incomplete"):
        store.stage_inline(b"abc", NODE, OTHER)
    store.workspace.unlink_managed_file.assert_called_once_with(
        Path("/workspace/tmp") / evidence.stage_name(NODE, OTHER)
    )
    with pytest.raises(InvalidParamsError):
        store.stage_inline(b"x" * (evidence.INLINE_LIMIT + 1), NODE, OTHER)


def test_publication_uses_already_durable_stage_without_reflushing_data(store):
    staged = store.stage_inline(b"abc", NODE, OTHER)
    store.publish(staged)
    assert isinstance(evidence.sync_evidence, Mock)
    evidence.sync_evidence.assert_called_once_with(10)
    store.workspace.publish.assert_called_once_with(staged.name, store.blob_name(staged.sha256), durable_stage=True)
    assert [call.args[0] for call in store.workspace.open_directory.call_args_list] == [
        store.workspace.evidence / staged.sha256[:2],
        store.workspace.evidence,
        store.workspace.tmp,
    ]


@pytest.mark.parametrize(
    ("pieces", "expected", "fails"),
    [
        ([b"a", b"bc", b""], [1, 2, 3, 4, 5], False),
        ([b"abcd"], [1, 2, 3, 4, 5], True),
        ([b"ab", b""], [1, 2, 3, 4, 5], True),
        ([], [1, 2, 4, 4, 5], True),
    ],
)
def test_copy_compares_descriptor_and_exact_byte_count(store, monkeypatch, pieces, expected, fails):
    monkeypatch.setattr(evidence.os, "read", Mock(side_effect=pieces))
    check = Mock()
    if fails:
        with pytest.raises(StorageIOError, match="SOURCE_CHANGED"):
            store.copy_path("source", expected, NODE, OTHER, check, short=True)
    else:
        result = store.copy_path("source", expected, NODE, OTHER, check, short=True)
        assert result.sha256 == hashlib.sha256(b"abc").hexdigest()
        assert result.byte_size == 3
        assert check.call_count == 4


def test_blob_verification_recheck_and_publication(store, monkeypatch):
    digest = hashlib.sha256(b"abc").hexdigest()
    monkeypatch.setattr(evidence.os, "read", Mock(side_effect=[b"abc", b""]))
    verified = store.verify_blob(digest, 3, Mock())
    assert verified is not None
    assert verified.identity == [1, 2, 3, 4, 5]
    store.recheck_blob(verified)
    store.publish(evidence.StagedEvidence("owned.stage", digest, 3))
    store.workspace.publish.assert_called_once_with(
        "owned.stage", f"{digest[:2]}/{digest[2:4]}/{digest}", durable_stage=True
    )
    assert store.workspace.open_directory.call_count == 3
    store.discard_stage(NODE, OTHER)
    store.unlink_blob(digest)
    assert store.workspace.unlink_managed_file.call_count == 2
    monkeypatch.setattr(evidence.os, "read", Mock(side_effect=[b"bad", b""]))
    with pytest.raises(StorageIOError, match="HASH_MISMATCH"):
        store.verify_blob(digest, 3, Mock())
    store.workspace.open_managed_file.side_effect = FileNotFoundError()
    assert store.verify_blob(digest, 3, Mock()) is None


def test_bucket_names_and_timeout_release_descriptor(store, monkeypatch):
    opened = Mock(return_value=7)
    monkeypatch.setattr(evidence, "open_lock", opened)
    flock = Mock()
    monkeypatch.setattr(evidence.fcntl, "flock", flock)
    with store.bucket("a" * 64, exclusive=False, deadline=float("inf")):
        assert opened.call_args.args[1] == "evidence-aaa.lock"
    assert isinstance(evidence.os.close, Mock)
    evidence.os.close.assert_called_once_with(7)
    flock.side_effect = BlockingIOError()
    with pytest.raises(BusyError):
        store.acquire_bucket("a" * 64, exclusive=True, deadline=0)
    assert evidence.os.close.call_count == 2
    with pytest.raises(InvalidParamsError):
        store.blob_name("../bad")


@pytest.mark.parametrize(("format_name", "content"), [("text", "abc"), ("base64", "YWJj")])
def test_read_slice_exact_range_and_representation(store, monkeypatch, format_name, content):
    monkeypatch.setattr(evidence.os, "pread", Mock(side_effect=[b"abc", b"abc"]))
    request = ReadEvidenceRequest(evidence_id=EVIDENCE, offset=0, length=10, format=format_name)
    result = store.read_slice("a" * 64, 3, "utf-8", request)
    assert result["content"] == content
    assert result["returned_range"] == {"offset": 0, "length": 3}
    with pytest.raises(InvalidParamsError):
        store.read_slice("a" * 64, 3, "utf-8", ReadEvidenceRequest(evidence_id=EVIDENCE, offset=4))


@pytest.mark.parametrize(
    ("raw", "prefix", "encoding", "offset", "expected"),
    [
        (b"", b"", "auto", 0, ""),
        (b"a\x00", b"\xff\xfe", "auto", 2, "a"),
        (b"\x00a", b"\xfe\xff", "auto", 2, "a"),
        (b"a", b"", "auto", 0, "a"),
    ],
)
def test_decode_range_encoding(raw, prefix, encoding, offset, expected):
    assert evidence._decode_range(raw, prefix, encoding, offset) == expected


@pytest.mark.parametrize(
    ("raw", "prefix", "encoding", "offset"),
    [(b"a", b"\xff\xfe\x00\x00", "auto", 0), (b"a\x00", b"", "utf-16le", 1), (b"\xff", b"", "utf-8", 0)],
)
def test_decode_range_rejects_unsupported_and_split_characters(raw, prefix, encoding, offset):
    with pytest.raises(InvalidParamsError):
        evidence._decode_range(raw, prefix, encoding, offset)


@pytest.mark.parametrize("state", ["pending", "ready", "not_applicable"])
def test_canonical_publish_records_provenance_targets_and_next_phase(monkeypatch, state):
    capability = claim(
        payload={
            "media_type": "text/plain",
            "encoding": "utf-8",
            "media_explicit": False,
            "encoding_explicit": False,
            "source": "probe",
            "warnings": [],
            "targets": [{"kind": "nodes", "id": NODE}, {"kind": "nodes", "id": OTHER}],
        }
    )
    monkeypatch.setattr(
        evidence_records.JobStore,
        "fence",
        Mock(return_value=job(blob_sha256="a" * 64, progress=json.dumps({"verified_sha256": "a" * 64, "bytes": 3}))),
    )
    record = owner(
        uuid=EVIDENCE,
        media_type="text/plain",
        encoding="utf-8",
        index_state=state,
        incomplete=int(state == "pending"),
        index_owner_job_id=None,
    )
    monkeypatch.setattr(evidence_records, "row_by_id", Mock(side_effect=[None, record, owner(), None]))
    finish, release = Mock(), Mock()
    monkeypatch.setattr(evidence_records.JobStore, "finish", finish)
    monkeypatch.setattr(evidence_records.JobStore, "release", release)
    result = evidence_records.EvidenceRecords.publish_record(database(), capability, "a" * 64, 3)
    assert result["warnings"] == ["TARGET_NOT_FOUND"]
    assert result["evidence_id"] == EVIDENCE
    assert release.call_count == int(state == "pending")
    assert finish.call_count == int(state != "pending")


def test_existing_evidence_metadata_conflict_and_cancellation(monkeypatch):
    monkeypatch.setattr(evidence_records.JobStore, "fence", Mock(return_value=job()))
    monkeypatch.setattr(
        evidence_records, "row_by_id", Mock(return_value=owner(media_type="text/plain", encoding="utf-8"))
    )
    capability = claim(payload={"media_explicit": True, "media_type": "application/json", "encoding_explicit": False})
    with pytest.raises(evidence_records.ConflictError, match="METADATA_CONFLICT"):
        evidence_records.EvidenceRecords.check_existing(database(), capability, "a" * 64)
    monkeypatch.setattr(evidence_records.JobStore, "fence", Mock(return_value=job(cancel_requested=1)))
    with pytest.raises(evidence_records.ConflictError, match="JOB_CANCELLED"):
        evidence_records.EvidenceRecords.publish_record(database(), capability, "a" * 64, 3)


def test_evidence_sync_requires_device_cache_flush_on_darwin(monkeypatch):
    calls = []
    monkeypatch.setattr(evidence.os, "fsync", Mock(side_effect=lambda fd: calls.append(("fsync", fd))))
    monkeypatch.setattr(evidence.sys, "platform", "darwin")
    monkeypatch.setattr(evidence.fcntl, "F_FULLFSYNC", 99, raising=False)
    monkeypatch.setattr(
        evidence.fcntl, "fcntl", Mock(side_effect=lambda fd, flag: calls.append(("fullsync", fd, flag)))
    )
    evidence.sync_evidence(9)
    assert calls == [("fsync", 9), ("fullsync", 9, 99)]
    monkeypatch.setattr(evidence.fcntl, "F_FULLFSYNC", None)
    with pytest.raises(StorageIOError, match="F_FULLFSYNC unavailable"):
        evidence.sync_evidence(9)


def test_source_stat_reports_unexpected_value_errors_as_io_error(store):
    # ValueError is not an OSError; without the widened handler it reaches the
    # client as INTERNAL rather than a documented evidence failure.
    store.workspace.open_import.side_effect = ValueError("open: embedded null character in path")
    with pytest.raises(StorageIOError, match="source unavailable") as failure:
        store.source_stat("input")
    assert failure.value.error_type == "IO_ERROR"


def test_inline_staging_checks_disk_reserve_once(store):
    # _stage already checked the same size; a second call only costs an fstatvfs.
    store.stage_inline(b"abc", NODE, OTHER)
    assert isinstance(evidence.CheckSpace, Mock)
    evidence.CheckSpace.assert_called_once_with(store.directory_fds, 3, store.policy)


@pytest.mark.parametrize("interval", [4096, 8 * 1024**2])
def test_copy_read_size_is_independent_of_the_disk_check_interval(store, monkeypatch, interval):
    sized = evidence.EvidenceStore(store.workspace, WorkspacePolicy(disk_check_interval_bytes=interval))
    read = Mock(side_effect=[b"abc", b""])
    monkeypatch.setattr(evidence.os, "read", read)
    sized.copy_path("source", [1, 2, 3, 4, 5], NODE, OTHER, Mock(), short=True)
    assert [call.args[1] for call in read.call_args_list] == [65536, 65536]
