"""Crash and native storage faults, without filling a user's filesystem."""

import asyncio
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import apsw
import pytest

from justpen_knowledgebase_mcp.config import ServerConfig, WorkspacePolicy
from justpen_knowledgebase_mcp.errors import ConfigurationError, StorageIOError
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage import evidence as evidence_module
from justpen_knowledgebase_mcp.storage.connection import SQLiteRuntime
from justpen_knowledgebase_mcp.workspace import WorkspacePaths

from .test_job_recovery import await_barrier, worker

pytestmark = pytest.mark.integration


async def test_actual_partial_path_copy_takeover_restarts_byte_zero(tmp_path, monkeypatch):
    raw = bytes(range(256)) * 8192
    source = tmp_path / "input.bin"
    source.write_bytes(raw)
    with worker(tmp_path, "partial_copy") as child:
        await asyncio.to_thread(await_barrier, child)
        stages = list(tmp_path.rglob("*.stage"))
        assert len(stages) == 1
        stale = stages[0]
        assert stale.read_bytes() == raw[:65536]
        child[0].kill()
        child[0].wait(timeout=10)
    writes = []
    original = evidence_module._write_all

    def observe(fd, content):
        writes.append(bytes(content))
        original(fd, content)

    cleaned = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_unlink = WorkspacePaths.unlink_managed_file

    def observe_unlink(workspace, path):
        original_unlink(workspace, path)
        if path == stale:
            loop.call_soon_threadsafe(cleaned.set)

    monkeypatch.setattr(evidence_module, "_write_all", observe)
    monkeypatch.setattr(WorkspacePaths, "unlink_managed_file", observe_unlink)
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        job_id, old_token = await kb.workers.control(lambda c, t: c.execute("select uuid,lease_token from jobs").get)
        await kb.workers.control(lambda c, t: c.execute("update jobs set lease_expires_at=0").fetchall())
        kb.job_runner.wake("bulk")
        result = await kb.job_runner.wait(job_id, time.monotonic() + 10)
        assert result["state"] == "completed", result
        assert result["evidence_id"] == "e_" + hashlib.sha256(raw).hexdigest()
        assert b"".join(writes) == raw, "takeover copies the entire input, rather than resuming the stale stage"
        assert writes[0] == raw[:65536]
        new_token = await kb.workers.read(lambda c, t: c.execute("select lease_token from jobs").get)
        assert new_token != old_token
        assert source.read_bytes() == raw
        # Startup can inspect this token before its lease expires. A protected
        # candidate is reconsidered by the 30-second scanner, independently of
        # the bulk lane publishing the replacement job's completed result.
        async with asyncio.timeout(35):
            await cleaned.wait()
        assert not stale.exists()
        blob = kb.workspace.evidence / kb.job_runner.store.blob_name(hashlib.sha256(raw).hexdigest())
        assert blob.read_bytes() == raw
        assert await kb.workers.read(lambda c, t: c.execute("pragma foreign_key_check").fetchall()) == []


@pytest.mark.parametrize("damage", [b"not a sqlite database", b"SQLite format 3\x00" + b"\x00" * 84])
def test_corrupt_and_truncated_database_fail_without_reinitializing(tmp_path, damage):
    config = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(config) as workspace:
        workspace.db.write_bytes(damage)
        with (
            SQLiteRuntime(workspace, config) as runtime,
            pytest.raises((apsw.Error, ConfigurationError, StorageIOError)),
        ):
            runtime.connect()
        assert workspace.db.read_bytes() == damage


async def test_native_sqlite_full_rolls_back_and_next_writer_remains_usable(kb):
    def fail(connection, _token):
        connection.execute("create table fault_payload(value)")
        connection.pragma("max_page_count", connection.pragma("page_count") + 1)
        connection.execute("insert into fault_payload values(zeroblob(8388608))")

    with pytest.raises(StorageIOError):
        await kb.workers.write(fail)
    assert (
        await kb.workers.read(lambda c, t: c.execute("select name from sqlite_master where name='fault_payload'").get)
        is None
    )
    assert (await kb.write({"nodes": [{"type": "hostname", "properties": {"name": "after-full"}}]}))["nodes"]


@pytest.mark.parametrize("boundary", ["before-copy", "eight-mib"])
def test_disk_reserve_fault_at_exact_copy_boundary_preserves_sources(tmp_path, monkeypatch, boundary):
    config = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(config) as workspace:
        store = evidence_module.EvidenceStore(workspace, WorkspacePolicy())
        raw = b"r" * (9 * 1048576)
        source = tmp_path / "source"
        source.write_bytes(raw)
        identity = store.source_stat("source")
        copied = 0
        original = evidence_module._write_all

        def write(fd, content):
            nonlocal copied
            original(fd, content)
            copied += len(content)

        def check(_descriptors, _size, _policy):
            if boundary == "before-copy" or copied >= 8 * 1048576:
                raise StorageIOError("IO_ERROR: DISK_RESERVE")

        monkeypatch.setattr(evidence_module, "CheckSpace", check)
        monkeypatch.setattr(evidence_module, "_write_all", write)
        with pytest.raises(StorageIOError, match="DISK_RESERVE") as from_error:
            store.copy_path(
                "source",
                identity,
                "00000000-0000-4000-8000-000000000001",
                "00000000-0000-4000-8000-000000000002",
                lambda: None,
            )
        assert from_error.value.error_type == "IO_ERROR"
        assert copied == (0 if boundary == "before-copy" else 8 * 1048576)
        assert source.read_bytes() == raw
        assert not list(workspace.tmp.glob("*.stage"))


def test_parallel_copy_reserve_failures_clean_only_their_owned_stages(tmp_path, monkeypatch):
    with WorkspacePaths(ServerConfig(workspace_dir=tmp_path)) as workspace:
        store = evidence_module.EvidenceStore(workspace, WorkspacePolicy())
        existing = store.stage_inline(b"canonical", str(uuid4()), str(uuid4()))
        store.publish(existing)
        for name in ["first", "second"]:
            (tmp_path / name).write_bytes(name.encode() * (2 * 1048576))
        reached = threading.Barrier(2)
        copied = threading.local()
        native = evidence_module._write_all

        def write(fd, data):
            native(fd, data)
            copied.bytes = getattr(copied, "bytes", 0) + len(data)
            if copied.bytes == 8 * 1048576:
                reached.wait(5)

        def check(_descriptors, _size, _policy):
            if getattr(copied, "bytes", 0) >= 8 * 1048576:
                raise StorageIOError("IO_ERROR: DISK_RESERVE")

        monkeypatch.setattr(evidence_module, "_write_all", write)
        monkeypatch.setattr(evidence_module, "CheckSpace", check)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    store.copy_path, name, store.source_stat(name), str(uuid4()), str(uuid4()), lambda: None
                )
                for name in ["first", "second"]
            ]
            for future in futures:
                with pytest.raises(StorageIOError, match="DISK_RESERVE"):
                    future.result(timeout=10)
        assert not list(workspace.tmp.glob("*.stage"))
        assert (workspace.evidence / store.blob_name(existing.sha256)).read_bytes() == b"canonical"
        for name in ["first", "second"]:
            assert (tmp_path / name).read_bytes() == name.encode() * (2 * 1048576)
