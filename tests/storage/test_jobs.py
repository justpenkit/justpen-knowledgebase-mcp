"""Durable job SQL fencing and real service evidence/delete lifecycles."""

import asyncio
import base64
import json
import os
import threading
import time
from uuid import uuid4

import apsw
import pytest

from justpen_knowledgebase_mcp.config import ServerConfig, WorkspacePolicy
from justpen_knowledgebase_mcp.errors import (
    BusyError,
    ConflictError,
    InvalidParamsError,
    LimitError,
    NotFoundError,
    RecordConflictError,
    StorageIOError,
)
from justpen_knowledgebase_mcp.evidence import IngestRequest
from justpen_knowledgebase_mcp.models import DeleteRequest, GetRequest, WriteRequest
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage import jobs
from justpen_knowledgebase_mcp.storage.evidence import EvidenceStore, stage_name
from justpen_knowledgebase_mcp.storage.job_recovery import recover_intents, staging_disposable
from justpen_knowledgebase_mcp.workspace import WorkspacePaths

pytestmark = pytest.mark.integration


async def test_fenced_claim_terminal_counters_and_retry(kb):
    await kb.job_runner.close()
    identifier = str(uuid4())
    await kb.workers.write(lambda c, t: jobs.JobStore.insert(c, identifier, "ingest", "bulk", {"fixture": True}))
    claim = await kb.workers.control(lambda c, t: jobs.JobStore.claim(c, "bulk", "ingest", now=100))
    assert claim.job_id == identifier
    assert claim.expires_at == 130
    assert await kb.workers.control(lambda c, t: jobs.JobStore.claim(c, "bulk", "ingest", now=129)) is None
    takeover = await kb.workers.control(lambda c, t: jobs.JobStore.claim(c, "bulk", "ingest", now=131))
    assert takeover.token != claim.token
    with pytest.raises(ConflictError):
        await kb.workers.control(lambda c, t: jobs.JobStore.finish(c, claim, "completed", {}, now=132))
    await kb.workers.control(lambda c, t: jobs.JobStore.finish(c, takeover, "failed", {"error": "IO_ERROR"}, now=132))
    counts = await kb.workers.read(lambda c, t: json.loads(c.execute("select terminal_job_counts from settings").get))
    assert counts == {"completed": 0, "failed_cancelled": 1}
    await kb.workers.control(lambda c, t: jobs.JobStore.retry(c, identifier))
    counts = await kb.workers.read(lambda c, t: json.loads(c.execute("select terminal_job_counts from settings").get))
    assert counts == {"completed": 0, "failed_cancelled": 0}


async def test_binary_ingest_read_dedup_sources_and_delete(kb):
    raw = base64.b64encode(b"\x00\xff\x10").decode()
    result = await kb.ingest_evidence({"base64": raw, "source": "scanner-a"})
    assert result["state"] == "completed"
    assert result["index_state"] == "not_applicable"
    assert result["effective_media_type"] == "application/octet-stream"
    identifier = result["evidence_id"]
    second = await kb.ingest_evidence({"base64": raw, "source": "scanner-b"})
    assert second["evidence_id"] == identifier
    page = await kb.get(GetRequest(kind="evidence", ids=[identifier], view="sources", limit=1))
    next_page = await kb.get(
        GetRequest(kind="evidence", ids=[identifier], view="sources", limit=1, cursor=page["next_cursor"])
    )
    assert {page["sources"][0]["source"], next_page["sources"][0]["source"]} == {"scanner-a", "scanner-b"}
    read = await kb.read_evidence({"evidence_id": identifier, "format": "base64"})
    assert read["content"] == raw
    deleted = await kb.delete(DeleteRequest(kind="evidence", ids=[identifier]))
    assert deleted["state"] == "completed"
    with pytest.raises(NotFoundError):
        await kb.read_evidence({"evidence_id": identifier})
    assert await kb.workers.read(lambda c, t: c.execute("select count(*) from evidence").get) == 0


async def test_delete_atomic_admission_cancel_boundary_and_recovery(kb):
    await kb.job_runner.close()
    output = await kb.write(
        WriteRequest.model_validate({"nodes": [{"type": "hostname", "properties": {"name": "pending"}}]})
    )
    identifier = output["nodes"][0]["id"]
    job = str(uuid4())
    request = DeleteRequest(kind="nodes", ids=[identifier])
    await kb.workers.write(lambda c, t: jobs.JobStore.admit_delete(c, request, job))
    with pytest.raises(ConflictError, match="DELETE_ALREADY_COMMITTED"):
        await kb.workers.control(lambda c, t: jobs.JobStore.cancel(c, job))
    with pytest.raises(RecordConflictError, match="RECORD_DELETING"):
        await kb.workers.write(lambda c, t: jobs.JobStore.admit_delete(c, request, str(uuid4())))
    await kb.workers.control(lambda c, t: c.execute("delete from jobs where uuid=?", (job,)).fetchall())
    repaired = await kb.workers.control(lambda c, t: recover_intents(c, "nodes", 0))
    assert repaired["repaired"] == 1
    restored = await kb.workers.read(lambda c, t: jobs.JobStore.get(c, job))
    assert restored["job_id"] == job
    assert restored["state"] == "queued"


async def test_missing_job_and_no_private_progress(kb):
    with pytest.raises(NotFoundError):
        await kb.jobs({"action": "get", "job_id": str(uuid4())})
    result = await kb.ingest_evidence({"base64": "AA=="})
    job = await kb.jobs({"action": "get", "job_id": result["job_id"]})
    assert set(job["progress"]) <= {"bytes", "chunks", "rows_deleted"}
    assert "stage_token" not in json.dumps(job)
    with pytest.raises(InvalidParamsError):
        await kb.jobs({"action": "list", "cursor": ""})


async def test_inline_failure_retains_input_then_retry_and_counter_equivalence(kb, monkeypatch):
    original = kb.job_runner.store.publish

    def fail(_staged):
        raise StorageIOError("IO_ERROR: injected publication failure")

    monkeypatch.setattr(kb.job_runner.store, "publish", fail)
    result = await kb.ingest_evidence({"base64": "AP8="})
    assert result["state"] == "failed"
    payload = await kb.workers.read(
        lambda c, t: json.loads(c.execute("select payload from jobs where uuid=?", (result["job_id"],)).get)
    )
    assert (kb.workspace.tmp / payload["input_stage"]).read_bytes() == b"\x00\xff"
    monkeypatch.setattr(kb.job_runner.store, "publish", original)
    await kb.jobs({"action": "retry", "job_id": result["job_id"]})
    completed = await kb.job_runner.wait(result["job_id"], time.monotonic() + 3)
    assert completed["state"] == "completed"
    assert completed["evidence_id"].startswith("e_")
    counts = await kb.workers.read(lambda c, t: json.loads(c.execute("select terminal_job_counts from settings").get))
    assert counts == {"completed": 1, "failed_cancelled": 0}


async def test_text_storage_is_durable_queued_handoff_not_completed(tmp_path):

    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path, query_timeout_ms=200)) as service:
        result = await service.ingest_evidence({"text": "raw awaits Task7", "media_type": "application/x-yaml"})
        assert result["status"] == "accepted"
        assert result["state"] == "queued"
        assert result["index_state"] == "pending"
        assert result["incomplete"] is True
        assert result["effective_media_type"] == "application/x-yaml"
        assert (await service.read_evidence({"evidence_id": result["evidence_id"]}))["content"] == "raw awaits Task7"
        count = await service.workers.read(
            lambda c, t: c.execute("select attempts from jobs where uuid=?", (result["job_id"],)).get
        )
        assert count == 1


async def test_ready_read_and_pending_delete_do_not_deadlock_short_lane(kb, monkeypatch):
    result = await kb.ingest_evidence({"base64": "AA=="})
    identifier = result["evidence_id"]
    ready = asyncio.Event()
    release = asyncio.Event()
    attempted = threading.Event()
    acquire = kb.job_runner.store.acquire_bucket

    def lock_attempt(*args, **kwargs):
        if kwargs.get("exclusive"):
            attempted.set()
        return acquire(*args, **kwargs)

    monkeypatch.setattr(kb.job_runner.store, "acquire_bucket", lock_attempt)
    original = kb.job_runner.workers.read

    async def barrier(callback, token=None):
        result = await original(callback, token)
        if getattr(callback, "__name__", "") == "metadata":
            ready.set()
            await release.wait()
        return result

    monkeypatch.setattr(kb.job_runner.workers, "read", barrier)
    read = asyncio.create_task(kb.read_evidence({"evidence_id": identifier, "format": "base64"}))
    await asyncio.wait_for(ready.wait(), 2)
    delete = asyncio.create_task(kb.delete(DeleteRequest(kind="evidence", ids=[identifier])))
    for _ in range(100):
        pending = await original(
            lambda c, t: c.execute("select lifecycle from evidence where uuid=?", (identifier,)).get
        )
        if pending == "delete_pending":
            break
        await asyncio.sleep(0)
    assert await asyncio.to_thread(attempted.wait, 2)
    release.set()
    assert (await asyncio.wait_for(read, 1))["content"] == "AA=="
    assert (await asyncio.wait_for(delete, 2))["state"] == "completed"


async def test_orphan_cleanup_rechecks_input_and_active_claim_ownership(kb):
    await kb.job_runner.close()
    identifier, input_token, old_token, active_token = (str(uuid4()) for _ in range(4))
    store = kb.job_runner.store
    for token in (input_token, old_token, active_token):
        store.stage_inline(b"safe", identifier, token)
    await kb.workers.control(
        lambda c, t: jobs.JobStore.insert(
            c,
            identifier,
            "ingest",
            "short",
            {"input_token": input_token, "input_stage": stage_name(identifier, input_token)},
        )
    )
    await kb.workers.control(
        lambda c, t: c.execute(
            "update jobs set state='running',lease_token=?,lease_expires_at=? where uuid=?",
            (active_token, time.time() + 30, identifier),
        ).fetchall()
    )
    for token, expected in ((input_token, False), (active_token, False), (old_token, True)):
        assert await kb.workers.control(lambda c, t, token=token: staging_disposable(c, identifier, token)) is expected
    assert await kb.workers.control(lambda c, t: staging_disposable(c, str(uuid4()), str(uuid4()))) is True


async def test_request_cancellation_during_inline_admission_cleans_only_owned_input(kb, monkeypatch):
    started, release = threading.Event(), threading.Event()
    original = kb.job_runner.store.stage_inline

    def barrier(*args):
        staged = original(*args)
        started.set()
        release.wait(3)
        return staged

    monkeypatch.setattr(kb.job_runner.store, "stage_inline", barrier)
    pending = asyncio.create_task(kb.ingest_evidence({"base64": "AA=="}))
    assert await asyncio.to_thread(started.wait, 2)
    pending.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert await kb.workers.read(lambda c, t: c.execute("select count(*) from jobs").get) == 0
    assert list(kb.workspace.tmp.glob("*.stage")) == []


async def test_startup_orphan_stage_cleanup_preserves_unrecognized_files(tmp_path):
    config = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(config) as workspace:
        store = EvidenceStore(workspace, WorkspacePolicy())
        orphan = store.stage_inline(b"orphan", str(uuid4()), str(uuid4()))
        unrelated = workspace.tmp / "not-a-job-stage"
        unrelated.write_bytes(b"retain")
    async with KnowledgeBase.open(config) as service:
        for _ in range(200):
            if not (service.workspace.tmp / orphan.name).exists():
                break
            await asyncio.sleep(0.005)
        assert not (service.workspace.tmp / orphan.name).exists()
        assert unrelated.read_bytes() == b"retain"


async def test_durable_cancelled_inline_retries_while_cancelled_waiter_does_not_cancel(kb, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = kb.job_runner._ingest_step

    async def barrier(claim):
        entered.set()
        await release.wait()
        await original(claim)

    monkeypatch.setattr(kb.job_runner, "_ingest_step", barrier)
    waiter = asyncio.create_task(kb.ingest_evidence({"base64": "AP8="}))
    await asyncio.wait_for(entered.wait(), 2)
    job_id = await kb.workers.read(lambda c, t: c.execute("select uuid from jobs").get)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert (await kb.jobs({"action": "get", "job_id": job_id}))["state"] == "running"
    await kb.jobs({"action": "cancel", "job_id": job_id})
    release.set()
    cancelled = await kb.job_runner.wait(job_id, time.monotonic() + 2)
    assert cancelled["state"] == "cancelled"
    assert len(list(kb.workspace.tmp.glob("*.stage"))) == 1
    await kb.jobs({"action": "retry", "job_id": job_id})
    completed = await kb.job_runner.wait(job_id, time.monotonic() + 2)
    assert completed["state"] == "completed"
    assert (await kb.read_evidence({"evidence_id": completed["evidence_id"], "format": "base64"}))["content"] == "AP8="


async def test_pending_evidence_rejects_new_read_import_link_and_delete(kb, monkeypatch):
    result = await kb.ingest_evidence({"base64": "AA=="})
    identifier = result["evidence_id"]
    entered, release = asyncio.Event(), asyncio.Event()
    original = kb.job_runner._delete_step

    async def barrier(claim):
        entered.set()
        await release.wait()
        await original(claim)

    monkeypatch.setattr(kb.job_runner, "_delete_step", barrier)
    deletion = asyncio.create_task(kb.delete(DeleteRequest(kind="evidence", ids=[identifier])))
    await asyncio.wait_for(entered.wait(), 2)
    with pytest.raises(RecordConflictError, match="RECORD_DELETING") as caught:
        await kb.read_evidence({"evidence_id": identifier})
    assert caught.value.details.blocking_record.id == identifier
    with pytest.raises(RecordConflictError, match="RECORD_DELETING"):
        await kb.write(
            WriteRequest.model_validate(
                {"nodes": [{"type": "hostname", "properties": {"name": "blocked"}, "evidence_add": [identifier]}]}
            )
        )
    with pytest.raises(RecordConflictError, match="RECORD_DELETING"):
        await kb.delete(DeleteRequest(kind="evidence", ids=[identifier]))
    release.set()
    assert (await deletion)["state"] == "completed"


@pytest.mark.parametrize("kind", ["nodes", "evidence"])
async def test_bulk_barrier_allows_short_ingest_and_batched_cleanup_fairness(kb, tmp_path, monkeypatch, kind):
    (tmp_path / "large.bin").write_bytes(b"\x00" * 300000)
    (tmp_path / "small.bin").write_bytes(b"small")
    entered, release_bulk = threading.Event(), threading.Event()
    original_copy = kb.job_runner.store.copy_path

    def copy(path, *args, **kwargs):
        if path == "large.bin":
            entered.set()
            release_bulk.wait(5)
            assert not kwargs.get("short")
        return original_copy(path, *args, **kwargs)

    monkeypatch.setattr(kb.job_runner.store, "copy_path", copy)
    large = await kb.ingest_evidence({"path": "large.bin"})
    assert large["status"] == "accepted"
    assert await asyncio.to_thread(entered.wait, 2)
    evidence_result = await kb.ingest_evidence({"base64": "AA=="})
    small = await kb.ingest_evidence({"path": "small.bin"})
    assert small["state"] == "completed"
    graph = await kb.write(
        WriteRequest.model_validate(
            {
                "nodes": [
                    {"type": "hostname", "properties": {"name": "hub"}},
                    {"type": "hostname", "properties": {"name": "leaf"}},
                ]
            }
        )
    )
    hub, leaf = [entry["id"] for entry in graph["nodes"]]

    def populate(c, _t):
        hub_id = c.execute("select id from nodes where uuid=?", (hub,)).get
        leaf_id = c.execute("select id from nodes where uuid=?", (leaf,)).get
        evidence_id = c.execute("select id from evidence where uuid=?", (evidence_result["evidence_id"],)).get
        for index in range(250):
            identifier = str(uuid4())
            if kind == "nodes":
                c.execute(
                    "insert into relations(uuid,source_id,target_id,type,key,properties) values(?,?,?,'resolves_to',?,'{}')",
                    (identifier, hub_id, leaf_id, str(index)),
                )
                owner = c.last_insert_rowid()
                c.execute("insert into relation_evidence(relation_id,evidence_id) values(?,?)", (owner, evidence_id))
            else:
                c.execute(
                    "insert into nodes(uuid,type,key,properties) values(?,'hostname',?,'{}')", (identifier, identifier)
                )
                c.execute(
                    "insert into node_evidence(node_id,evidence_id) values(?,?)", (c.last_insert_rowid(), evidence_id)
                )

    await kb.workers.write(populate)
    first_step, release_step = asyncio.Event(), asyncio.Event()
    original_delete = kb.job_runner._delete_step
    step_count = 0
    order = []
    original_ingest = kb.job_runner._ingest_step

    async def ingest_step(claim):
        order.append("ingest")
        await original_ingest(claim)

    monkeypatch.setattr(kb.job_runner, "_ingest_step", ingest_step)

    async def delete_step(claim):
        nonlocal step_count
        order.append("delete")
        await original_delete(claim)
        step_count += 1
        if step_count == 1:
            first_step.set()
            await release_step.wait()

    monkeypatch.setattr(kb.job_runner, "_delete_step", delete_step)
    identifier = hub if kind == "nodes" else evidence_result["evidence_id"]
    deletion = asyncio.create_task(kb.delete(DeleteRequest(kind=kind, ids=[identifier], cascade=True)))
    await asyncio.wait_for(first_step.wait(), 2)
    queued = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_insert = jobs.JobStore.insert

    def accepted(*args):
        original_insert(*args)
        loop.call_soon_threadsafe(queued.set)

    monkeypatch.setattr(jobs.JobStore, "insert", staticmethod(accepted))
    inline = asyncio.create_task(kb.ingest_evidence({"base64": "AQ=="}))
    await asyncio.wait_for(queued.wait(), 2)
    release_step.set()
    assert (await asyncio.wait_for(inline, 2))["state"] == "completed"
    assert (await asyncio.wait_for(deletion, 2))["state"] == "completed"
    assert order[:3] == ["delete", "ingest", "delete"]
    assert step_count >= 3
    assert not release_bulk.is_set()
    release_bulk.set()
    assert (await kb.job_runner.wait(large["job_id"], time.monotonic() + 3))["state"] == "completed"


async def test_shutdown_cancellation_keeps_workspace_until_io_drains(tmp_path, monkeypatch):
    (tmp_path / "large.bin").write_bytes(b"\x00" * 300000)
    context = KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path))
    kb = await context.__aenter__()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    observed = []
    original = kb.job_runner.store.copy_path

    def blocked(*args, **kwargs):
        entered.set()
        release.wait(3)
        observed.append(kb.workspace.root_fd)
        try:
            return original(*args, **kwargs)
        finally:
            observed.append(kb.workspace.root_fd)
            finished.set()

    monkeypatch.setattr(kb.job_runner.store, "copy_path", blocked)
    await kb.ingest_evidence({"path": "large.bin"})
    assert await asyncio.to_thread(entered.wait, 2)
    shutdown = asyncio.create_task(context.__aexit__(None, None, None))
    await kb.job_runner._stop_event.wait()
    shutdown.cancel()
    await asyncio.sleep(0)
    shutdown.cancel()
    try:
        await asyncio.sleep(0)
        assert kb.workspace.root_fd >= 0
        assert not shutdown.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
    assert await asyncio.to_thread(finished.wait, 2)
    assert observed
    assert all(value >= 0 for value in observed)
    assert kb.workspace.root_fd == -1


async def test_full_database_is_io_error_and_failure_commit_never_reports_success(kb, tmp_path, monkeypatch):
    def full(_connection, _token):
        raise apsw.FullError("controlled fixture disk full")

    with pytest.raises(StorageIOError):
        await kb.workers.control(full)

    (tmp_path / "disk.bin").write_bytes(b"\x00" * 300000)
    original = jobs.JobStore.finish
    failed = asyncio.Event()
    loop = asyncio.get_running_loop()
    attempts = 0

    def fail_finish(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            loop.call_soon_threadsafe(failed.set)
        raise apsw.FullError("controlled fixture full during failure commit")

    monkeypatch.setattr(jobs.JobStore, "finish", staticmethod(fail_finish))
    accepted = await kb.ingest_evidence({"path": "disk.bin"})
    await asyncio.wait_for(failed.wait(), 2)
    for _ in range(100):
        if kb.job_runner.last_error:
            break
        await asyncio.sleep(0)
    assert kb.job_runner.last_error == "IO_ERROR: job failure could not be committed"
    state = await kb.jobs({"action": "get", "job_id": accepted["job_id"]})
    assert state["state"] == "running"
    assert await kb.workers.read(lambda c, t: c.execute("select count(*) from evidence").get) == 0
    monkeypatch.setattr(jobs.JobStore, "finish", staticmethod(original))
    await kb.workers.control(
        lambda c, t: c.execute("update jobs set lease_expires_at=0 where uuid=?", (accepted["job_id"],)).fetchall()
    )
    kb.job_runner.wake("bulk")
    recovered = await kb.job_runner.wait(accepted["job_id"], time.monotonic() + 3)
    assert recovered["state"] == "completed"
    assert (tmp_path / "disk.bin").stat().st_size == 300000


async def test_conflicting_media_and_pending_dedup_preserve_raw_source_metadata(kb, monkeypatch):
    original = await kb.ingest_evidence({"base64": "AP8=", "media_type": "image/png", "source": "original"})
    conflict = await kb.ingest_evidence(
        {"base64": "AP8=", "media_type": "application/octet-stream", "source": "rejected"}
    )
    assert conflict["state"] == "failed"
    assert conflict["error"] == "CONFLICT"
    source = await kb.get(GetRequest(kind="evidence", ids=[original["evidence_id"]], view="sources"))
    assert [entry["source"] for entry in source["sources"]] == ["original"]
    job_id = str(uuid4())
    request = DeleteRequest(kind="evidence", ids=[original["evidence_id"]])

    def admit_fixture(c, _t):
        jobs.JobStore.admit_delete(c, request, job_id)
        c.execute("update jobs set kind='fixture' where uuid=?", (job_id,))

    await kb.workers.write(admit_fixture)
    duplicate = await kb.ingest_evidence({"base64": "AP8=", "media_type": "image/png"})
    assert duplicate["state"] == "failed"
    assert duplicate["reason"] == "RECORD_DELETING"
    assert duplicate["details"]["blocking_record"]["id"] == original["evidence_id"]


async def test_job_list_budget_keeps_whole_rows_and_resumable_cursor(kb):
    ids = ["e_" + f"{index:064x}" for index in range(100)]

    def populate(c, _t):
        for _ in range(60):
            job_id = str(uuid4())
            jobs.JobStore.insert(c, job_id, "delete", "bulk", {})
            claim = jobs.JobStore.claim(c, "bulk", "delete")
            assert claim is not None
            jobs.JobStore.finish(c, claim, "completed", {"deleted_ids": ids})

    await kb.workers.control(populate)
    first = await kb.jobs({"action": "list", "limit": 100})
    assert len(json.dumps(first).encode()) <= 256 * 1024
    assert first["next_cursor"] is not None
    second = await kb.jobs({"action": "list", "limit": 100, "cursor": first["next_cursor"]})
    assert len(first["jobs"]) + len(second["jobs"]) == 60
    assert not ({row["job_id"] for row in first["jobs"]} & {row["job_id"] for row in second["jobs"]})


async def test_inline_decode_runs_off_event_loop(kb, monkeypatch):
    event_loop_thread = threading.get_ident()
    observed = []
    original = base64.b64decode

    def decode(*args, **kwargs):
        observed.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(base64, "b64decode", decode)
    assert (await kb.ingest_evidence({"base64": "AP8="}))["state"] == "completed"
    assert observed
    assert event_loop_thread not in observed


async def test_failed_delete_retains_intent_requires_attention_and_retries(kb, monkeypatch):
    result = await kb.ingest_evidence({"base64": "AP8="})
    identifier = result["evidence_id"]
    original = kb.job_runner.store.unlink_blob

    def fail(_digest):
        raise StorageIOError("IO_ERROR: fixture unlink failure")

    monkeypatch.setattr(kb.job_runner.store, "unlink_blob", fail)
    deletion = await kb.delete(DeleteRequest(kind="evidence", ids=[identifier]))
    assert deletion["state"] == "failed"
    assert deletion["needs_attention"] is True
    with pytest.raises(ConflictError, match="DELETE_ALREADY_COMMITTED"):
        await kb.jobs({"action": "cancel", "job_id": deletion["job_id"]})
    pending = await kb.get(GetRequest(kind="evidence", ids=[identifier]))
    assert pending["records"][0]["delete_job_id"] == deletion["job_id"]
    monkeypatch.setattr(kb.job_runner.store, "unlink_blob", original)
    await kb.jobs({"action": "retry", "job_id": deletion["job_id"]})
    assert (await kb.job_runner.wait(deletion["job_id"], time.monotonic() + 2))["state"] == "completed"
    with pytest.raises(ConflictError, match="DELETE_ALREADY_COMMITTED"):
        await kb.jobs({"action": "cancel", "job_id": deletion["job_id"]})


async def test_delete_request_cancel_before_admission_commit_rolls_back_every_owner(kb, monkeypatch):
    output = await kb.write(
        WriteRequest.model_validate({"nodes": [{"type": "hostname", "properties": {"name": "atomic"}}]})
    )
    identifier = output["nodes"][0]["id"]
    entered, release = threading.Event(), threading.Event()
    original = jobs.JobStore.admit_delete

    def barrier(*args):
        original(*args)
        entered.set()
        release.wait(2)

    monkeypatch.setattr(jobs.JobStore, "admit_delete", staticmethod(barrier))
    deletion = asyncio.create_task(kb.delete(DeleteRequest(kind="nodes", ids=[identifier])))
    assert await asyncio.to_thread(entered.wait, 2)
    deletion.cancel()
    with pytest.raises(asyncio.CancelledError):
        await deletion
    release.set()
    assert await kb.workers.control(lambda c, t: c.execute("select count(*) from jobs").get) == 0
    record = (await kb.get(GetRequest(kind="nodes", ids=[identifier])))["records"][0]
    assert record["lifecycle"] == "ready"
    assert record["delete_job_id"] is None


async def test_io_lane_admission_is_bounded_without_blocking_other_lane(kb):
    entered, release = threading.Event(), threading.Event()

    def blocked():
        entered.set()
        release.wait(3)

    first = asyncio.create_task(kb.job_runner.io("bulk", blocked))
    assert await asyncio.to_thread(entered.wait, 2)
    queued = [asyncio.create_task(kb.job_runner.io("bulk", lambda: None)) for _ in range(31)]
    await asyncio.sleep(0)
    try:
        with pytest.raises(BusyError):
            await kb.job_runner.io("bulk", lambda: None)
        assert await kb.job_runner.io("short", lambda: "short remains usable") == "short remains usable"
        assert kb.job_runner._pending["bulk"] == 32
    finally:
        release.set()
        await asyncio.gather(first, *queued)
    assert kb.job_runner._pending["bulk"] == 0


async def test_queued_text_result_is_incomplete_before_raw_storage(kb):
    job_id = str(uuid4())

    def queued(c, _t):
        jobs.JobStore.insert(c, job_id, "ingest", "bulk", {"media_type": "text/plain"})
        result = jobs.JobStore.get(c, job_id)
        c.execute("delete from jobs where uuid=?", (job_id,))
        return result

    result = await kb.workers.control(queued)
    assert result["index_state"] == "pending"
    assert result["incomplete"] is True


async def test_failed_text_storage_reports_failed_status(kb, monkeypatch):
    def fail(*_args):
        raise StorageIOError("IO_ERROR: controlled text stage failure")

    monkeypatch.setattr(kb.job_runner, "_copy_input", fail)
    result = await kb.ingest_evidence({"text": "failed before publication"})
    assert result["state"] == "failed"
    assert result["status"] == "failed"
    assert result["incomplete"] is True


async def test_periodic_recovery_survives_transient_busy(kb, monkeypatch):
    calls = []

    async def recover():
        calls.append("recovery")
        if len(calls) == 1:
            raise BusyError("controlled recovery contention")
        kb.job_runner._stopping = True

    original_wait = asyncio.wait_for

    async def timer(awaitable, **kwargs):
        interval = kwargs["timeout"]
        if interval == 30:
            task = asyncio.create_task(awaitable)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            raise TimeoutError
        return await original_wait(awaitable, interval)

    monkeypatch.setattr(kb.job_runner, "recover", recover)
    monkeypatch.setattr(asyncio, "wait_for", timer)
    await kb.job_runner._recovery_loop()
    assert calls == ["recovery", "recovery"]


async def test_stopped_runner_does_not_retry_lock_admission_until_deadline(kb):
    await kb.job_runner.close()
    with pytest.raises(BusyError):
        await asyncio.wait_for(kb.job_runner.acquire_bucket("short", "a" * 64, exclusive=True), 0.1)


async def test_missing_targets_warn_without_losing_binary_storage(kb):
    result = await kb.ingest_evidence(
        {"base64": "AA==", "targets": [{"kind": "nodes", "id": str(uuid4())} for _ in range(100)]}
    )
    assert result["state"] == "completed"
    assert result["warnings"].count("TARGET_NOT_FOUND") == 100
    assert "MEDIA_TYPE_DEFAULTED_TEXT_INDEX_SKIPPED" in result["warnings"]
    assert (await kb.read_evidence({"evidence_id": result["evidence_id"], "format": "base64"}))["content"] == "AA=="


async def test_explicit_encoding_conflict_is_not_silently_overridden(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path, query_timeout_ms=200)) as kb:
        original = await kb.ingest_evidence({"text": "encoding", "encoding": "utf-8"})
        result = await kb.ingest_evidence({"text": "encoding", "encoding": "latin-1"})
        assert result["state"] == "failed"
        assert result["error"] == "CONFLICT"
        record = (await kb.get(GetRequest(kind="evidence", ids=[original["evidence_id"]])))["records"][0]
        assert record["encoding"] == "utf-8"


async def test_first_oversized_job_list_item_raises_limit(kb, monkeypatch):

    job_id = str(uuid4())
    await kb.workers.write(lambda c, t: jobs.JobStore.insert(c, job_id, "ingest", "bulk", {}))
    real_get = jobs.JobStore.get

    def oversized(connection, identifier):
        result = real_get(connection, identifier)
        result["warnings"] = ["\x00" * 256] * 200
        return result

    monkeypatch.setattr(jobs.JobStore, "get", oversized)
    with pytest.raises(LimitError, match="response"):
        await kb.jobs({"limit": 1})


async def test_foreground_validation_queue_expires_before_native_admission(tmp_path):

    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path, query_timeout_ms=100)) as kb:
        entered, release = threading.Event(), threading.Event()

        def blocked():
            entered.set()
            release.wait(3)

        active = asyncio.create_task(kb.job_runner.io("short", blocked))
        assert await asyncio.to_thread(entered.wait, 2)
        request = asyncio.create_task(kb.ingest_evidence({"base64": "AA=="}))
        try:
            with pytest.raises(LimitError):
                await asyncio.wait_for(asyncio.shield(request), 0.5)
            assert await kb.workers.read(lambda c, t: c.execute("select count(*) from jobs").get) == 0
        finally:
            release.set()
            await active
            await asyncio.gather(request, return_exceptions=True)


async def test_accumulated_validation_and_bucket_delay_share_deadline(tmp_path, monkeypatch):

    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path, query_timeout_ms=200)) as kb:
        original = IngestRequest.model_validate
        attempts = []

        def slow_validation(value):
            time.sleep(0.14)
            return original(value)

        def unavailable(*args, **kwargs):
            attempts.append(time.monotonic())
            raise BusyError("fixture bucket occupied")

        monkeypatch.setattr(IngestRequest, "model_validate", slow_validation)
        monkeypatch.setattr(kb.job_runner.store, "acquire_bucket", unavailable)
        start = time.monotonic()
        with pytest.raises((BusyError, LimitError)):
            await kb.ingest_evidence({"base64": "AA=="})
        assert attempts
        assert time.monotonic() - start < 0.30
        assert await kb.workers.read(lambda c, t: c.execute("select count(*) from jobs").get) == 0


@pytest.mark.parametrize("expired_after_commit", [False, True])
async def test_post_acceptance_busy_returns_retained_metadata(tmp_path, monkeypatch, expired_after_commit):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path, query_timeout_ms=100)) as kb:
        original_write = kb.workers.write
        original_read = kb.workers.read
        accepted = False
        foreground = asyncio.current_task()
        read_calls = 0

        async def committed(callback, token=None):
            nonlocal accepted
            result = await original_write(callback, token)
            accepted = True
            if expired_after_commit:
                await asyncio.sleep(0.12)
            return result

        async def contended(callback, token=None):
            nonlocal read_calls
            if asyncio.current_task() is foreground:
                read_calls += 1
            if accepted:
                raise BusyError("fixture metadata reader contention")
            return await original_read(callback, token)

        monkeypatch.setattr(kb.workers, "write", committed)
        monkeypatch.setattr(kb.workers, "read", contended)
        result = await kb.ingest_evidence({"text": "durable input"})
        assert result["status"] == "accepted"
        assert result["kind"] == "ingest"
        assert result["effective_media_type"] == "text/plain"
        assert read_calls == 0 if expired_after_commit else read_calls > 0
        assert (
            await original_read(
                lambda c, t: c.execute("select count(*) from jobs where uuid=?", (result["job_id"],)).get
            )
            == 1
        )


async def test_post_acceptance_native_db_timeout_returns_snapshot(tmp_path, monkeypatch):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path, query_timeout_ms=100)) as kb:
        original_write, original_read = kb.workers.write, kb.workers.read
        committed = False
        entered, release = threading.Event(), threading.Event()

        async def write(callback, token=None):
            nonlocal committed
            result = await original_write(callback, token)
            committed = True
            return result

        async def read(callback, token=None):
            if not committed:
                return await original_read(callback, token)

            def delayed(connection, operation):
                entered.set()
                release.wait(2)
                return callback(connection, operation)

            return await original_read(delayed, token)

        monkeypatch.setattr(kb.workers, "write", write)
        monkeypatch.setattr(kb.workers, "read", read)
        try:
            result = await asyncio.wait_for(kb.ingest_evidence({"text": "durable queued input"}), 0.5)
            assert entered.is_set()
            assert not release.is_set()
            assert result["status"] == "accepted"
            assert result["job_id"]
        finally:
            release.set()
        assert (
            await original_read(
                lambda c, t: c.execute("select count(*) from jobs where uuid=?", (result["job_id"],)).get
            )
            == 1
        )


async def test_raw_read_bucket_and_database_share_deadline(tmp_path, monkeypatch):

    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path, query_timeout_ms=200)) as kb:
        result = await kb.ingest_evidence({"base64": "AA=="})
        acquire, read = kb.job_runner.store.acquire_bucket, kb.workers.read

        def delayed_bucket(*args, **kwargs):
            time.sleep(0.14)
            return acquire(*args, **kwargs)

        async def delayed_metadata(callback, token=None):
            if getattr(callback, "__name__", "") != "metadata":
                return await read(callback, token)

            def delayed(connection, operation):
                time.sleep(0.14)
                return callback(connection, operation)

            return await read(delayed, token)

        monkeypatch.setattr(kb.job_runner.store, "acquire_bucket", delayed_bucket)
        monkeypatch.setattr(kb.workers, "read", delayed_metadata)
        start = time.monotonic()
        with pytest.raises(LimitError):
            await kb.read_evidence({"evidence_id": result["evidence_id"]})
        assert time.monotonic() - start < 0.27


async def test_cancelled_native_raw_read_retains_bucket_until_drain(kb, monkeypatch):
    result = await kb.ingest_evidence({"base64": "AA=="})
    entered, release = threading.Event(), threading.Event()
    original = kb.job_runner.store.read_slice
    digest = result["evidence_id"][2:]

    def delayed(*args):
        entered.set()
        release.wait(2)
        return original(*args)

    monkeypatch.setattr(kb.job_runner.store, "read_slice", delayed)
    task = asyncio.create_task(kb.read_evidence({"evidence_id": result["evidence_id"]}))
    assert await asyncio.to_thread(entered.wait, 2)
    try:
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        with pytest.raises(BusyError):
            kb.job_runner.store.acquire_bucket(digest, exclusive=True, deadline=time.monotonic())
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    fd = kb.job_runner.store.acquire_bucket(digest, exclusive=True, deadline=time.monotonic())

    os.close(fd)


async def test_expired_io_entries_keep_queue_bound_until_consumed(kb):
    entered, release = threading.Event(), threading.Event()
    invoked = []

    def blocked():
        entered.set()
        release.wait(3)

    active = asyncio.create_task(kb.job_runner.io("bulk", blocked))
    assert await asyncio.to_thread(entered.wait, 2)
    deadline = time.monotonic() + 0.05
    queued = [asyncio.create_task(kb.job_runner.io("bulk", lambda: invoked.append(True), deadline)) for _ in range(31)]
    try:
        results = await asyncio.gather(*queued, return_exceptions=True)
        assert all(isinstance(result, LimitError) for result in results)
        assert kb.job_runner._pending["bulk"] == 32
        with pytest.raises(BusyError):
            await kb.job_runner.io("bulk", lambda: None)
    finally:
        release.set()
        await active
    for _ in range(100):
        if kb.job_runner._pending["bulk"] == 0:
            break
        await asyncio.sleep(0.001)
    assert kb.job_runner._pending["bulk"] == 0
    assert invoked == []


async def test_inline_native_expiry_drains_then_cleans_unaccepted_input(tmp_path, monkeypatch):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path, query_timeout_ms=100)) as kb:
        stage = kb.job_runner.store.stage_inline

        def delayed(*args):
            result = stage(*args)
            time.sleep(0.15)
            return result

        monkeypatch.setattr(kb.job_runner.store, "stage_inline", delayed)
        with pytest.raises(LimitError):
            await kb.ingest_evidence({"base64": "AA=="})
        assert await kb.workers.read(lambda c, t: c.execute("select count(*) from jobs").get) == 0
        assert list(kb.workspace.tmp.iterdir()) == []


async def test_facade_raw_text_json_expansion_rejects_exact_range(kb):
    raw = b"\x00" * 65536
    result = await kb.ingest_evidence({"base64": base64.b64encode(raw).decode()})
    request = {"evidence_id": result["evidence_id"], "length": len(raw)}
    with pytest.raises(LimitError, match="response"):
        await kb.read_evidence(request)
    read = await kb.read_evidence({**request, "format": "base64"})
    assert read["returned_range"] == {"offset": 0, "length": len(raw)}
    assert base64.b64decode(read["content"]) == raw
    assert len(json.dumps({"status": "ok", "data": read}).encode()) < 262144
