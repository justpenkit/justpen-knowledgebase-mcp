"""Descriptor-based reserve checks on managed copy and database devices."""

import errno
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from justpen_knowledgebase_mcp.config import ServerConfig, WorkspacePolicy
from justpen_knowledgebase_mcp.errors import StorageIOError
from justpen_knowledgebase_mcp.storage import disk_budget
from justpen_knowledgebase_mcp.storage.evidence import EvidenceStore
from justpen_knowledgebase_mcp.workspace import WorkspacePaths


@pytest.mark.integration
def test_descriptor_measurement_available_blocks_and_single_device(tmp_path, monkeypatch):
    policy = WorkspacePolicy()
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    calls = []

    def measure(descriptor):
        calls.append(descriptor)
        return SimpleNamespace(f_bavail=policy.disk_reserve_bytes + 10, f_frsize=1, f_bfree=10**20)

    monkeypatch.setattr(os, "fstatvfs", measure)
    try:
        disk_budget.CheckSpace((fd, fd), 10, policy)
        assert calls == [fd]
        with pytest.raises(StorageIOError, match="DISK_RESERVE"):
            disk_budget.CheckSpace((fd, fd), 11, policy)
    finally:
        os.close(fd)


def test_distinct_database_device_requires_reserve_not_input(monkeypatch):
    policy = WorkspacePolicy()
    monkeypatch.setattr(os, "fstat", lambda fd: SimpleNamespace(st_dev=fd))
    monkeypatch.setattr(
        os,
        "fstatvfs",
        lambda fd: SimpleNamespace(f_bavail=policy.disk_reserve_bytes + (100 if fd == 1 else 0), f_frsize=1),
    )
    disk_budget.CheckSpace((1, 2), 100, policy)
    with pytest.raises(StorageIOError, match="DISK_RESERVE"):
        disk_budget.CheckSpace((2, 1), 100, policy)


def test_measurement_failure_is_io_error(monkeypatch):
    def fail(_fd):
        raise OSError("private pathname")

    monkeypatch.setattr(os, "fstat", fail)
    with pytest.raises(StorageIOError, match="measurement failed") as caught:
        disk_budget.CheckSpace((1, 2), 100, WorkspacePolicy())
    assert "private pathname" not in str(caught.value)


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["reserve", "measurement", "enospc"])
def test_copy_fault_cleans_only_its_stage_and_preserves_source_and_published_blob(tmp_path, monkeypatch, failure):
    config = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(config) as workspace:
        store = EvidenceStore(workspace, WorkspacePolicy())
        existing = store.stage_inline(b"ready", str(uuid4()), str(uuid4()))
        store.publish(existing)
        source = tmp_path / "source.bin"
        source.write_bytes(b"source" * 30000)
        before = source.read_bytes()
        expected = store.source_stat("source.bin")
        calls = 0

        def measure(_fd):
            nonlocal calls
            calls += 1
            if failure == "measurement":
                raise OSError("fixture unavailable")
            available = store.policy.disk_reserve_bytes + len(before) if calls < 3 else 0
            return SimpleNamespace(f_bavail=available, f_frsize=1)

        def no_space(_fd, _data):
            raise OSError(errno.ENOSPC, "fixture full")

        if failure == "enospc":
            monkeypatch.setattr(os, "write", no_space)
        else:
            monkeypatch.setattr(os, "fstatvfs", measure)
        with pytest.raises(StorageIOError):
            store.copy_path("source.bin", expected, str(uuid4()), str(uuid4()), lambda: None)
        assert source.read_bytes() == before
        assert (workspace.evidence / store.blob_name(existing.sha256)).read_bytes() == b"ready"
        assert list(workspace.tmp.glob("*.stage")) == []
