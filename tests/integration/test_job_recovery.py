"""Process-kill and real transaction barriers; these are not power-loss proofs."""

import asyncio
import hashlib
import json
import os
import select
import subprocess
import sys
import time
from contextlib import contextmanager
from uuid import uuid4

import pytest

from justpen_knowledgebase_mcp import jobs as job_module
from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import BusyError, LimitError
from justpen_knowledgebase_mcp.models import DeleteRequest, GetRequest, WriteRequest
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.job_recovery import recover_intents
from justpen_knowledgebase_mcp.storage.jobs import PENDING_SQL, JobStore

pytestmark = pytest.mark.integration


@contextmanager
def worker(root, scenario):
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-m",
            "tests.integration.job_worker",
            str(root),
            scenario,
            str(ready_write),
            str(release_read),
        ],
        pass_fds=(ready_write, release_read),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    os.close(ready_write)
    os.close(release_read)
    try:
        yield process, ready_read, release_write
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        os.close(ready_read)
        os.close(release_write)


def await_barrier(item):
    process, ready, _release = item
    assert select.select([ready], [], [], 10)[0], f"barrier timeout, returncode={process.poll()}"
    assert os.read(ready, 1) == b"1"


@pytest.mark.parametrize("phase", ["before_hash", "published", "ready", "unlink"])
async def test_kill_boundaries_recover_owned_evidence_and_delete(tmp_path, phase):
    raw = b"\x00\xff" * 200000
    (tmp_path / "input.bin").write_bytes(raw)
    identifier = "e_" + hashlib.sha256(raw).hexdigest()
    with worker(tmp_path, phase) as child:
        await asyncio.to_thread(await_barrier, child)
        child[0].kill()
        child[0].wait(timeout=10)
    if phase == "published":
        # The trusted persisted hash allows reusing the completed owned blob,
        # even though unfinished path retries must restart from byte zero.
        (tmp_path / "input.bin").write_bytes(b"changed source")
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        await kb.workers.control(
            lambda c, t: c.execute("update jobs set lease_expires_at=0 where state='running'").fetchall()
        )
        kb.job_runner.wake("short")
        kb.job_runner.wake("bulk")
        ids = await kb.workers.read(lambda c, t: [row[0] for row in c.execute("select uuid from jobs order by id")])
        for job_id in ids:
            result = await kb.job_runner.wait(job_id, time.monotonic() + 5)
            assert result["state"] == "completed", result
        records = await kb.get(GetRequest(kind="evidence", ids=[identifier]))
        if phase == "unlink":
            assert records["missing_ids"] == [identifier]
        else:
            assert len(records["records"]) == 1
            result = await kb.read_evidence({"evidence_id": identifier, "format": "base64", "offset": 399990})
            assert result["returned_range"]["length"] == 10
        assert await kb.workers.read(lambda c, t: c.execute("pragma foreign_key_check").fetchall()) == []


async def test_two_processes_same_blob_one_identity(tmp_path):
    raw = b"\x00\xff" * 200000
    (tmp_path / "input.bin").write_bytes(raw)
    with worker(tmp_path, "race") as first, worker(tmp_path, "race") as second:
        await asyncio.gather(asyncio.to_thread(await_barrier, first), asyncio.to_thread(await_barrier, second))
        os.write(first[2], b"1")
        os.write(second[2], b"1")
        outputs = await asyncio.gather(
            asyncio.to_thread(first[0].communicate, timeout=10), asyncio.to_thread(second[0].communicate, timeout=10)
        )
        results = [json.loads(stdout) for stdout, _stderr in outputs]
        assert all(not stderr for _stdout, stderr in outputs)
        assert results[0]["evidence_id"] == results[1]["evidence_id"]
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        assert await kb.workers.read(lambda c, t: c.execute("select count(*) from evidence").get) == 1
        assert await kb.workers.read(lambda c, t: c.execute("pragma foreign_key_check").fetchall()) == []


async def test_pre_admission_input_is_protected_from_other_process_orphan_cleanup(tmp_path):
    with worker(tmp_path, "admission") as child:
        await asyncio.to_thread(await_barrier, child)
        async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
            stages = list(kb.workspace.tmp.glob("*.stage"))
            assert len(stages) == 1
            # The second process's cleanup waits outside SQL for the first
            # process's admission bucket. A DB read remains independently usable.
            assert await kb.workers.read(lambda c, t: c.execute("select count(*) from jobs").get) == 0
            assert stages[0].read_bytes() == b"\x00\xff"
            os.write(child[2], b"1")
            stdout, stderr = await asyncio.to_thread(child[0].communicate, timeout=10)
            result = json.loads(stdout)
            assert not stderr
            assert result["state"] == "completed"
            assert (await kb.read_evidence({"evidence_id": result["evidence_id"], "format": "base64"}))[
                "content"
            ] == "AP8="


async def test_heartbeat_retries_real_writer_contention_without_losing_lease(tmp_path, monkeypatch):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path, db_busy_timeout_ms=20)) as kb:
        job_id = str(uuid4())
        await kb.workers.control(lambda c, t: JobStore.insert(c, job_id, "fixture", "bulk", {}))
        claim = await kb.workers.control(lambda c, t: JobStore.claim(c, "bulk", "fixture"))
        assert claim is not None
        monkeypatch.setattr(job_module, "HEARTBEAT_SECONDS", 0.01)
        attempts = 0
        retried = asyncio.Event()
        original = kb.workers.control

        async def observe(callback, token=None):
            nonlocal attempts
            try:
                return await original(callback, token)
            except (BusyError, LimitError):
                if "heartbeat" in callback.__code__.co_names:
                    attempts += 1
                if attempts >= 2:
                    retried.set()
                raise

        with worker(tmp_path, "writer") as child:
            await asyncio.to_thread(await_barrier, child)
            monkeypatch.setattr(kb.workers, "control", observe)
            initial_expiry = claim.expires_at
            heartbeat = asyncio.create_task(kb.job_runner._heartbeat(claim))
            await asyncio.wait_for(retried.wait(), 3)
            assert not claim.lost
            assert claim.expires_at == initial_expiry
            os.write(child[2], b"1")
            await asyncio.to_thread(child[0].communicate, timeout=10)
            for _ in range(200):
                if claim.expires_at > initial_expiry:
                    break
                await asyncio.sleep(0.005)
            assert claim.expires_at > initial_expiry
            assert not claim.lost
            heartbeat.cancel()
            with pytest.raises(asyncio.CancelledError):
                await heartbeat


async def test_recovery_keyset_reaches_owner101_with_million_ready_rows(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        await kb.job_runner.close()
        jobs = [str(uuid4()) for _ in range(101)]

        def populate(connection, _token):
            connection.execute(
                "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000) INSERT INTO nodes(uuid,type,key,properties) SELECT printf('%036d',x),'hostname',cast(x as text),'{}' FROM n"
            )
            for index, job_id in enumerate(jobs):
                identifier = str(uuid4())
                connection.execute(
                    "INSERT INTO nodes(uuid,type,key,properties,lifecycle,delete_job_id,delete_cascade,delete_requested_at) VALUES(?,'hostname',?,'{}','delete_pending',?,1,1)",
                    (identifier, identifier, job_id),
                )
                if index < 100:
                    JobStore.insert(connection, job_id, "fixture", "short", {})
                    claim = JobStore.claim(connection, "short", "fixture")
                    assert claim is not None
                    JobStore.finish(connection, claim, "failed", {})
            return connection.execute("EXPLAIN QUERY PLAN " + PENDING_SQL["nodes"], (0,)).fetchall()

        plan = await kb.workers.control(populate)
        assert any("nodes_pending_owner" in row[3] for row in plan)
        first = await kb.workers.control(lambda c, t: recover_intents(c, "nodes", 0))
        assert first["repaired"] == 0
        assert first["after_id"] > 1000000
        second = await kb.workers.control(lambda c, t: recover_intents(c, "nodes", first["after_id"]))
        assert second == {"after_id": 0, "repaired": 1}
        assert (
            await kb.workers.control(lambda c, t: c.execute("select state from jobs where uuid=?", (jobs[-1],)).get)
            == "queued"
        )
        assert (
            await kb.workers.control(
                lambda c, t: c.execute("select count(*) from nodes where lifecycle='delete_pending'").get
            )
            == 101
        )


@pytest.mark.parametrize("scenario", ["claim", "stale_claim"])
async def test_two_process_claim_and_expired_delete_worker_cannot_commit(tmp_path, scenario):
    job_id = str(uuid4())
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        owner = (
            await kb.write(
                WriteRequest.model_validate({"nodes": [{"type": "hostname", "properties": {"name": "delete-owner"}}]})
            )
        )["nodes"][0]["id"]

        def prepare(c, _t):
            JobStore.admit_delete(c, DeleteRequest(kind="nodes", ids=[owner]), job_id)
            c.execute("update jobs set kind='fixture' where uuid=?", (job_id,))

        await kb.workers.write(prepare)
    with worker(tmp_path, scenario) as first:
        await asyncio.to_thread(await_barrier, first)
        if scenario == "claim":
            with worker(tmp_path, scenario) as second:
                await asyncio.to_thread(await_barrier, second)
                os.write(first[2], b"1")
                os.write(second[2], b"1")
                output = await asyncio.gather(
                    asyncio.to_thread(first[0].communicate, timeout=10),
                    asyncio.to_thread(second[0].communicate, timeout=10),
                )
                tokens = [json.loads(stdout)["token"] for stdout, _stderr in output]
                assert sum(token is not None for token in tokens) == 1
        else:
            async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
                claim = await kb.workers.control(lambda c, t: JobStore.claim(c, "short", "fixture"))
                assert claim is not None
                await kb.workers.control(lambda c, t: JobStore.delete_step(c, claim))
                assert (await kb.get(GetRequest(kind="nodes", ids=[owner])))["missing_ids"] == [owner]
                os.write(first[2], b"1")
                stdout, stderr = await asyncio.to_thread(first[0].communicate, timeout=10)
                assert not stderr
                assert json.loads(stdout) == {"fenced": True}


@pytest.mark.parametrize("phase", ["admission", "inline_accepted"])
async def test_inline_kill_before_and_after_acceptance_reconciles_owned_input(tmp_path, phase):
    with worker(tmp_path, phase) as child:
        await asyncio.to_thread(await_barrier, child)
        child[0].kill()
        child[0].wait(timeout=10)
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        rows = await kb.workers.read(lambda c, t: c.execute("select uuid,payload from jobs").fetchall())
        if phase == "admission":
            assert rows == []
            for path in kb.workspace.tmp.glob("*.stage"):
                await kb.job_runner._orphan_step(path.name)
            assert list(kb.workspace.tmp.glob("*.stage")) == []
        else:
            assert len(rows) == 1
            job_id, payload = rows[0]
            assert isinstance(job_id, str)
            assert isinstance(payload, str)
            options = json.loads(payload)
            assert (kb.workspace.tmp / options["input_stage"]).read_bytes() == b"\x00\xff"
            assert "base64" not in options
            await kb.workers.control(
                lambda c, t: c.execute("update jobs set lease_expires_at=0 where uuid=?", (job_id,)).fetchall()
            )
            kb.job_runner.wake("short")
            result = await kb.job_runner.wait(job_id, time.monotonic() + 3)
            assert result["state"] == "completed"
            assert (await kb.read_evidence({"evidence_id": result["evidence_id"], "format": "base64"}))[
                "content"
            ] == "AP8="
