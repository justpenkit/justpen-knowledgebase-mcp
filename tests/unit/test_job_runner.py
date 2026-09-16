"""Runner orchestration with DB, evidence store and OS ownership isolated."""

import asyncio
import threading
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from opentelemetry.context import Context, attach, detach

from justpen_knowledgebase_mcp import jobs
from justpen_knowledgebase_mcp.config import WorkspacePolicy
from justpen_knowledgebase_mcp.errors import BusyError, ConflictError, LimitError, StorageIOError
from justpen_knowledgebase_mcp.evidence import IngestRequest, ReadEvidenceRequest
from justpen_knowledgebase_mcp.models import DeleteRequest
from justpen_knowledgebase_mcp.storage.evidence import stage_name
from justpen_knowledgebase_mcp.telemetry.context import extract_carrier

from .helpers import EVIDENCE, NODE, OTHER, claim, cursor, database, job, owner


@pytest.fixture
async def runner(monkeypatch):
    monkeypatch.setattr(jobs, "EvidenceStore", Mock())
    monkeypatch.setattr(jobs, "StageScan", Mock())
    monkeypatch.setattr(jobs.os, "close", Mock())
    db = database()

    async def dispatch(callback, token=None):
        return callback(db, token or Mock())

    workers = Mock(factory=SimpleNamespace(config=SimpleNamespace(query_timeout_ms=10000)))
    workers.read = AsyncMock(side_effect=dispatch)
    workers.write = AsyncMock(side_effect=dispatch)
    workers.control = AsyncMock(side_effect=dispatch)
    value = jobs.JobRunner(workers, Mock(), WorkspacePolicy())
    value.store.policy = WorkspacePolicy()
    value.store.workspace.tmp = Path("/workspace/tmp")

    async def io(_lane, callback, _deadline=None):
        return callback()

    value.io = AsyncMock(side_effect=io)
    value.acquire_bucket = AsyncMock(return_value=9)
    yield value
    await value.close()


def test_io_call_abandonment_and_started_owner():
    callback = Mock(return_value=3)
    call = jobs._IOCall(callback, None)
    assert call.abandon_queued()
    with pytest.raises(LimitError):
        call.run()
    callback.assert_not_called()
    call = jobs._IOCall(callback, float("inf"))
    assert call.run() == 3
    assert not call.abandon_queued()


async def test_real_io_lane_capacity_and_shutdown(monkeypatch):
    monkeypatch.setattr(jobs, "EvidenceStore", Mock())
    monkeypatch.setattr(jobs, "StageScan", Mock())
    value = jobs.JobRunner(Mock(), Mock(), WorkspacePolicy())
    try:
        assert await value.io("short", lambda: 4) == 4
        await asyncio.sleep(0)
        assert value.queue_status()["short"]["pending"] == 0
        value._pending["bulk"] = 32
        with pytest.raises(BusyError):
            await value.io("bulk", lambda: None)
        value._pending["bulk"] = 0
        for _ in range(40):
            value.wake("short")
        assert value._wake["short"].qsize() == 32
    finally:
        await value.close()
    with pytest.raises(BusyError):
        await value.io("short", lambda: None)
    assert value.queue_status()["stopping"]


async def test_ingest_inline_acceptance_and_failure_cleanup(runner, monkeypatch):
    staged = SimpleNamespace(name=stage_name(NODE, OTHER), byte_size=3)
    runner.store.stage_inline.return_value = staged
    monkeypatch.setattr(jobs.JobStore, "insert", Mock())
    monkeypatch.setattr(jobs.JobStore, "get", Mock(return_value={"job_id": NODE, "state": "queued", "lane": "short"}))
    runner.wait = AsyncMock(side_effect=lambda _id, _deadline, accepted: accepted)
    result = await runner.ingest(IngestRequest(text="abc"), float("inf"))
    assert result["status"] == "accepted"
    assert isinstance(jobs.JobStore.insert, Mock)
    options = jobs.JobStore.insert.call_args.args[-1]
    assert "text" not in options
    assert options["input_size"] == 3
    assert isinstance(jobs.os.close, Mock)
    jobs.os.close.assert_called_once_with(9)
    monkeypatch.setattr(jobs.JobStore, "insert", Mock(side_effect=StorageIOError("failed")))
    with pytest.raises(StorageIOError):
        await runner.ingest(IngestRequest(text="abc"), float("inf"))
    runner.store.discard_stage.assert_called_once()


@pytest.mark.parametrize("size", [3, 300000])
async def test_path_admission_selects_lane_without_inline_copy(runner, monkeypatch, size):
    runner.store.source_stat.return_value = (1, 2, size, 3, 4)
    runner.store.workspace.relative.return_value = Path("source.txt")
    monkeypatch.setattr(jobs.JobStore, "insert", Mock())
    monkeypatch.setattr(jobs.JobStore, "get", Mock(return_value={"state": "queued"}))
    runner.wait = AsyncMock(side_effect=lambda _id, _deadline, accepted: accepted)
    assert (await runner.ingest(IngestRequest(path="source.txt"), float("inf")))["status"] == "accepted"
    assert isinstance(jobs.JobStore.insert, Mock)
    assert jobs.JobStore.insert.call_args.args[3] == ("short" if size == 3 else "bulk")
    assert runner.wait.call_count == int(size == 3)


async def test_delete_controls_and_wait_preserve_accepted_metadata(runner, monkeypatch):
    monkeypatch.setattr(jobs.JobStore, "admit_delete", Mock())
    monkeypatch.setattr(jobs.JobStore, "get", Mock(return_value={"state": "completed", "lane": "short"}))
    assert (await runner.delete(DeleteRequest(kind="nodes", ids=[NODE]), float("inf")))["status"] == "completed"
    for action in ("get", "cancel", "retry"):
        if action != "get":
            monkeypatch.setattr(jobs.JobStore, action, Mock(return_value={"lane": "short"}))
        result = await runner.control(jobs.JobsRequest(action=action, job_id=NODE), float("inf"))
        assert result["lane"] == "short"
    assert await runner.wait(NODE, 0, {"status": "accepted"}) == {"status": "accepted"}
    with pytest.raises(LimitError):
        await runner.wait(NODE, 0)
    runner.workers.read.side_effect = BusyError("busy")
    with pytest.raises(BusyError):
        await runner.wait(NODE, float("inf"))


async def test_read_holds_bucket_through_metadata_and_slice(runner, monkeypatch):
    monkeypatch.setattr(jobs, "row_by_id", Mock(return_value=owner(byte_size=3, encoding="utf-8")))
    runner.store.read_slice.return_value = {"text": "abc"}
    request = ReadEvidenceRequest(evidence_id=EVIDENCE)
    assert await runner.read(request, float("inf")) == {"text": "abc"}
    runner.store.read_slice.assert_called_once_with("a" * 64, 3, "utf-8", request)
    assert isinstance(jobs.os.close, Mock)
    jobs.os.close.assert_called_once_with(9)
    monkeypatch.setattr(jobs, "row_by_id", Mock(return_value=None))
    with pytest.raises(jobs.NotFoundError):
        await runner.read(request, float("inf"))
    assert jobs.os.close.call_count == 2


async def test_recovery_and_cleanup_rotation(runner, monkeypatch):
    monkeypatch.setattr(jobs, "recover_intents", Mock(return_value={"after_id": 100}))
    runner._stages.batch.return_value = [stage_name(NODE, OTHER)]
    await runner.recover()
    assert set(runner._recovery_after.values()) == {100}
    assert list(runner._orphans) == [stage_name(NODE, OTHER)]
    monkeypatch.setattr(jobs.JobRetention, "next_job", Mock(return_value=None))
    monkeypatch.setattr(jobs.JobStore, "claim", Mock(return_value=None))
    runner._orphan_step = AsyncMock()
    assert await runner._cleanup_step()
    runner._orphan_step.assert_awaited_once_with(stage_name(NODE, OTHER))
    assert not await runner._cleanup_step()
    assert not await runner._category_step("short", "ingest")


async def test_purge_and_orphan_acknowledge_after_unlink(runner, monkeypatch):
    calls = []
    monkeypatch.setattr(jobs.JobRetention, "next_file", Mock(return_value=OTHER))
    monkeypatch.setattr(jobs.JobRetention, "acknowledge", Mock(side_effect=lambda *_args: calls.append("ack")))
    monkeypatch.setattr(jobs.JobRetention, "finalize", Mock(side_effect=lambda *_args: calls.append("finalize")))
    runner.store.discard_stage.side_effect = lambda *_args: calls.append("unlink")
    await runner._purge_step(NODE)
    assert calls == ["unlink", "ack", "finalize"]
    monkeypatch.setattr(jobs, "staging_disposable", Mock(return_value=True))
    monkeypatch.setattr(
        jobs.JobRetention, "forget_clean_input", Mock(side_effect=lambda *_args: calls.append("forget"))
    )
    await runner._orphan_step(stage_name(NODE, OTHER))
    assert calls[-2:] == ["unlink", "forget"]


async def test_retention_cache_success_and_failure(runner, monkeypatch):
    monkeypatch.setattr(jobs.JobRetention, "counts", Mock(return_value={"completed": 0, "failed_cancelled": 0}))
    monkeypatch.setattr(jobs.JobRetention, "batch", Mock(return_value={"cursor": (0.0, 0), "marked": 1}))
    monkeypatch.setattr(
        jobs.JobRetention,
        "snapshot",
        Mock(
            return_value={
                "terminal_counts": {"completed": 0, "failed_cancelled": 0},
                "protected_count": 0,
                "needs_attention": False,
                "pending_prune_count": 1,
                "pruned_total": 3,
            }
        ),
    )
    assert not await runner.retention_pass(force=True)
    snapshot = runner.retention_status()
    assert snapshot["available"]
    assert not snapshot["stale"]
    assert snapshot["pruned_total"] == 3
    assert not await runner.retention_pass()
    assert isinstance(jobs.JobRetention.batch, Mock)
    assert jobs.JobRetention.batch.call_count == 1
    runner.workers.read.side_effect = StorageIOError("secret")
    with pytest.raises(StorageIOError):
        await runner.retention_pass()
    assert runner.retention_status()["stale"]


@pytest.mark.parametrize("kind", ["ingest", "reindex", "delete"])
async def test_claim_dispatch_and_heartbeat_ownership_cleanup(runner, monkeypatch, kind):
    runner._ingest_step = AsyncMock()
    runner._delete_step = AsyncMock()
    monkeypatch.setattr(jobs, "reindex_step", AsyncMock())
    capability = claim(kind=kind)
    await runner._run_claim(capability)
    assert not runner._claims
    assert runner._retention_event.is_set()
    callback = (
        runner._ingest_step if kind == "ingest" else runner._delete_step if kind == "delete" else jobs.reindex_step
    )
    assert isinstance(callback, AsyncMock)
    callback.assert_awaited_once()


async def test_ingest_publication_orders_blob_before_canonical_metadata(runner, monkeypatch):
    calls = []
    staged = SimpleNamespace(sha256="a" * 64, byte_size=3)
    runner._copy_input = AsyncMock(return_value=staged)
    monkeypatch.setattr(jobs.JobStore, "checkpoint", Mock(side_effect=lambda *_args: calls.append("checkpoint")))
    monkeypatch.setattr(jobs.EvidenceRecords, "check_existing", Mock(side_effect=lambda *_args: calls.append("check")))
    monkeypatch.setattr(jobs.EvidenceRecords, "publish_record", Mock(side_effect=lambda *_args: calls.append("record")))
    runner.store.publish.side_effect = lambda *_args: calls.append("blob")
    await runner._ingest_step(claim(payload={"input_token": OTHER}))
    assert calls == ["checkpoint", "check", "blob", "record"]
    assert list(runner._orphans) == [stage_name(NODE, OTHER)]
    calls.clear()
    runner.store.verify_blob.return_value = staged
    runner.store.recheck_blob.side_effect = lambda *_args: calls.append("recheck")
    await runner._ingest_step(claim(progress={"verified_sha256": "a" * 64, "bytes": 3}))
    assert calls == ["check", "recheck", "record"]


async def test_copy_source_and_input_ownership(runner, monkeypatch):
    capability = claim(payload={"path": "source", "source_stat": (1, 2, 3)})
    runner.store.copy_path.return_value = "staged"
    assert await runner._copy_input(capability) == "staged"
    capability = claim(payload={"input_stage": "foreign", "input_token": OTHER})
    with pytest.raises(StorageIOError, match="ownership"):
        await runner._copy_input(capability)
    capability.payload["input_stage"] = stage_name(NODE, OTHER)
    runner.store.workspace.open_managed_file.return_value = nullcontext(7)
    monkeypatch.setattr(jobs.os, "read", Mock(return_value=b"abc"))
    runner.store.stage_inline.return_value = "staged"
    assert await runner._copy_input(capability) == "staged"
    runner.store.stage_inline.assert_called_once_with(b"abc", NODE, OTHER)


async def test_delete_files_validate_before_unlink_and_finalization(runner, monkeypatch):
    calls = []
    monkeypatch.setattr(jobs.JobStore, "fence", Mock(return_value=job()))
    monkeypatch.setattr(jobs, "row_by_id", Mock(return_value=owner(lifecycle="delete_pending", delete_job_id=NODE)))
    monkeypatch.setattr(jobs.JobStore, "finalize_evidence", Mock(side_effect=lambda *_args: calls.append("finalize")))
    runner.store.unlink_blob.side_effect = lambda *_args: calls.append("unlink")
    await runner._delete_step(claim(kind="delete", progress={"files_pending": EVIDENCE}))
    assert calls == ["unlink", "finalize"]
    monkeypatch.setattr(jobs, "row_by_id", Mock(return_value=owner()))
    with pytest.raises(ConflictError):
        await runner._delete_step(claim(kind="delete", progress={"files_pending": EVIDENCE}))
    assert calls == ["unlink", "finalize"]


async def test_failure_sanitizes_io_and_preserves_input(runner, monkeypatch, capsys):
    monkeypatch.setattr(jobs.JobStore, "fence", Mock(return_value=job()))
    finish = Mock()
    monkeypatch.setattr(jobs.JobStore, "finish_failure", finish)
    capability = claim(progress={"awaiting_text_index": True})
    await runner._fail_claim(capability, OSError("secret file path"))
    assert finish.call_args.args[2] == "failed"
    result = finish.call_args.args[3]
    assert result["reason"] == "managed I/O failed"
    assert result["incomplete"]
    runner.store.discard_stage.assert_called_once_with(NODE, OTHER)
    runner._failure_cache("safe diagnostic")
    runner._failure_cache("safe diagnostic")
    assert capsys.readouterr().err == "safe diagnostic\n"


def test_list_jobs_binds_filter_and_paginates(monkeypatch):
    monkeypatch.setattr(jobs.JobStore, "get", Mock(side_effect=[{"job_id": NODE}, {"job_id": OTHER}]))
    db = database(cursor(value=(NODE, 1)), cursor(rows=[(1, NODE), (2, OTHER)]))
    result = jobs._list_jobs(db, jobs.JobsRequest(limit=1, state="failed"))
    assert result["jobs"] == [{"job_id": NODE}]
    assert result["next_cursor"]
    assert db.execute.call_args.args[1] == (0, "failed", "failed", 2)


async def test_cancelled_queued_io_keeps_capacity_until_executor_consumes(monkeypatch):

    monkeypatch.setattr(jobs, "EvidenceStore", Mock())
    monkeypatch.setattr(jobs, "StageScan", Mock())
    value = jobs.JobRunner(Mock(), Mock(), WorkspacePolicy())
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def hold_owner():
        loop.call_soon_threadsafe(started.set)
        release.wait()
        return "owned"

    active = asyncio.create_task(value.io("short", hold_owner))
    queued_callback = Mock()
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        queued = asyncio.create_task(value.io("short", queued_callback))
        await asyncio.sleep(0)
        assert value._pending["short"] == 2
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert value._pending["short"] == 2
        release.set()
        assert await active == "owned"
        await value.close_io_owner("short", lambda: None)
        await asyncio.sleep(0)
        assert value._pending["short"] == 0
        queued_callback.assert_not_called()
    finally:
        release.set()
        await value.close()


async def test_cancelled_active_io_drains_owner_before_return(monkeypatch):

    monkeypatch.setattr(jobs, "EvidenceStore", Mock())
    monkeypatch.setattr(jobs, "StageScan", Mock())
    value = jobs.JobRunner(Mock(), Mock(), WorkspacePolicy())
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    completed = []

    def hold_owner():
        loop.call_soon_threadsafe(started.set)
        release.wait()
        completed.append("closed")

    task = asyncio.create_task(value.io("bulk", hold_owner))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert completed == ["closed"]
    finally:
        release.set()
        await value.close()


async def test_bucket_cancelled_acquisition_closes_eventual_descriptor(runner):
    started, release = asyncio.Event(), asyncio.Event()

    async def acquire(_lane, _callback, _deadline):
        started.set()
        await release.wait()
        return 7

    runner.io.side_effect = acquire
    task = asyncio.create_task(
        jobs.JobRunner.acquire_bucket(runner, "short", "a" * 64, exclusive=True, deadline=float("inf"))
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert isinstance(jobs.os.close, Mock)
    jobs.os.close.assert_called_once_with(7)


async def test_heartbeat_retries_busy_then_renews_capability(runner, monkeypatch):
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(jobs.asyncio, "sleep", sleep)
    capability = claim()
    heartbeat = Mock(side_effect=[BusyError("busy"), (float("inf"), True)])
    monkeypatch.setattr(jobs.JobStore, "heartbeat", heartbeat)
    dispatch = runner.workers.control.side_effect

    async def control(callback, token=None):
        result = await dispatch(callback, token)
        runner._stopping = True
        return result

    runner.workers.control.side_effect = control
    await runner._heartbeat(capability)
    assert capability.cancelled
    assert not capability.lost
    assert heartbeat.call_count == 2
    assert delays[0] == jobs.HEARTBEAT_SECONDS
    assert 0.25 <= delays[1] <= 0.274


async def test_heartbeat_rejects_lost_claim_and_expiry(runner, monkeypatch):
    monkeypatch.setattr(jobs.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(jobs.JobStore, "heartbeat", Mock(side_effect=ConflictError("CLAIM_LOST")))
    capability = claim()
    await runner._heartbeat(capability)
    assert capability.lost
    expired = claim(expires_at=0)
    await runner._heartbeat(expired)
    assert expired.lost


async def test_run_claim_failure_always_releases_heartbeat_owner(runner):
    capability = claim()
    runner._ingest_step = AsyncMock(side_effect=StorageIOError("managed"))
    runner._fail_claim = AsyncMock()
    await runner._run_claim(capability)
    runner._fail_claim.assert_awaited_once()
    assert not runner._claims


async def test_ingest_index_continuation_merges_existing_warnings(runner, monkeypatch):
    monkeypatch.setattr(
        jobs, "index_evidence", AsyncMock(return_value={"warnings": ["TOKEN_TOO_LONG"], "incomplete": True})
    )
    finish = Mock()
    monkeypatch.setattr(jobs.JobStore, "finish", finish)
    capability = claim(
        progress={"awaiting_text_index": True, "evidence_id": EVIDENCE},
        result={"warnings": ["TARGET_NOT_FOUND"], "evidence_id": EVIDENCE},
    )
    await runner._ingest_step(capability)
    assert finish.call_args.args[3] == {
        "warnings": ["TARGET_NOT_FOUND", "TOKEN_TOO_LONG"],
        "incomplete": True,
        "evidence_id": EVIDENCE,
    }
    runner.store.publish.assert_not_called()


@pytest.mark.parametrize(
    ("lane", "expected"), [("short", ["ingest", "cleanup", "ingest"]), ("bulk", ["ingest", "reindex", "ingest"])]
)
async def test_lane_alternates_ready_work_classes(runner, lane, expected):
    categories = []

    async def step(_lane, category):
        categories.append(category)
        if len(categories) == 3:
            runner._stopping = True
        return True

    runner._category_step = AsyncMock(side_effect=step)
    await runner._lane(lane)
    assert categories == expected


async def test_background_admission_failure_is_sanitized_and_loop_recovers(runner):
    calls = []

    async def step(_lane, _category):
        calls.append(1)
        if len(calls) == 1:
            raise StorageIOError("private")
        runner._stopping = True
        return True

    runner._category_step = AsyncMock(side_effect=step)
    runner._failure_cache = Mock()
    runner.wake("short")
    await runner._lane("short")
    runner._failure_cache.assert_called_once_with("IO_ERROR: background job admission failed")
    assert len(calls) == 2


async def test_start_recovers_before_starting_background_owners(runner, monkeypatch):
    phases = []
    runner.recover = AsyncMock(side_effect=lambda: phases.append("recover"))
    monkeypatch.setattr(jobs.JobRetention, "reconcile", Mock(side_effect=lambda _c: phases.append("reconcile")))
    runner.retention_pass = AsyncMock(side_effect=lambda **_kwargs: phases.append("retention"))
    runner._lane = AsyncMock()
    runner._recovery_loop = AsyncMock()
    runner._retention_loop = AsyncMock()
    await runner.start()
    assert phases == ["recover", "reconcile", "retention"]
    assert len(runner._tasks) == 4
    await runner.close()
    assert all(task.done() for task in runner._tasks)


@pytest.mark.parametrize("failure", [None, BusyError("busy"), LimitError("budget"), StorageIOError("private")])
async def test_periodic_recovery_failure_classification_and_stop(runner, failure):
    runner._stop_event = Mock(wait=AsyncMock(side_effect=TimeoutError()))

    async def recover():
        runner._stopping = True
        if failure is not None:
            raise failure

    runner.recover = AsyncMock(side_effect=recover)
    runner._failure_cache = Mock()
    await runner._recovery_loop()
    runner.recover.assert_awaited_once()
    if isinstance(failure, StorageIOError):
        runner._failure_cache.assert_called_once_with("IO_ERROR: periodic job recovery failed")
    else:
        runner._failure_cache.assert_not_called()


@pytest.mark.parametrize("failure", [None, BusyError("busy"), StorageIOError("private")])
async def test_retention_loop_wakes_after_each_bounded_pass(runner, failure):
    async def retention():
        runner._stopping = True
        runner._retention_event.set()
        if failure is not None:
            raise failure
        return True

    runner.retention_pass = AsyncMock(side_effect=retention)
    runner._failure_cache = Mock()
    await runner._retention_loop()
    runner.retention_pass.assert_awaited_once()
    if isinstance(failure, StorageIOError):
        runner._failure_cache.assert_called_once_with("IO_ERROR: job retention failed")
    else:
        runner._failure_cache.assert_not_called()


async def test_ingest_context_captured_before_worker_dispatch(runner, monkeypatch):

    parent = "00-" + "11" * 16 + "-" + "22" * 8 + "-01"
    inserted = []
    monkeypatch.setattr(jobs.JobStore, "insert", lambda _c, _id, _kind, _lane, payload: inserted.append(payload))
    monkeypatch.setattr(jobs.JobStore, "get", lambda *_args: {"state": "queued"})

    async def worker(callback, operation):
        token = attach(Context())
        try:
            return callback(Mock(), operation)
        finally:
            detach(token)

    runner.workers.write.side_effect = worker
    token = attach(extract_carrier({"traceparent": parent}).context)
    try:
        await runner._accept_ingest(NODE, "bulk", {"path": "sentinel-secret"}, float("inf"))
    finally:
        detach(token)
    assert inserted[0]["_telemetry"] == {"traceparent": parent}


@pytest.mark.parametrize("error", [BusyError("capacity"), LimitError("deadline")])
async def test_admission_interruption_retains_durable_job(runner, monkeypatch, error):
    capability = claim(progress={"bytes": 1})
    runner._ingest_step = AsyncMock(side_effect=error)
    runner._fail_claim = AsyncMock()
    monkeypatch.setattr(jobs.JobStore, "fence", Mock(return_value=job(progress='{"bytes":42}')))
    released = Mock()
    monkeypatch.setattr(jobs.JobStore, "release", released)
    await runner._run_claim(capability)
    runner._fail_claim.assert_not_awaited()
    runner.store.discard_stage.assert_not_called()
    assert released.call_args.args[2] == {"bytes": 42}
    assert not runner._claims


@pytest.mark.parametrize("error", [BusyError("pressure"), LimitError("capacity")])
async def test_start_defers_optional_retention_sampling(runner, monkeypatch, error):
    runner.recover = AsyncMock()
    monkeypatch.setattr(jobs.JobRetention, "reconcile", Mock())
    runner.workers.read.side_effect = error
    runner._lane = AsyncMock()
    runner._recovery_loop = AsyncMock()
    runner._retention_loop = AsyncMock()
    await runner.start()
    assert len(runner._tasks) == 4
    assert runner.retention_status()["stale"]
    assert not runner.retention_status()["available"]


@pytest.mark.parametrize("failure", [BusyError("reset"), LimitError("deadline"), ConflictError("CLAIM_LOST")])
async def test_pressure_deferral_keeps_lease_when_control_unavailable(runner, failure):
    runner._stop_event.set()
    runner.workers.control.side_effect = failure
    await runner._defer_claim(claim(), BusyError("pressure"))
    runner.store.discard_stage.assert_not_called()
    assert runner.last_error is None


@pytest.mark.parametrize(("stopping", "lost"), [(True, False), (False, True)])
async def test_pressure_backoff_shutdown_and_lost_claim_do_not_release(runner, stopping, lost):
    runner._stop_event.set()
    runner._stopping = stopping
    await runner._defer_claim(claim(lost=lost), BusyError("pressure"))
    runner.workers.control.assert_not_awaited()


async def test_start_does_not_hide_permanent_sampling_failure(runner, monkeypatch):
    runner.recover = AsyncMock()
    monkeypatch.setattr(jobs.JobRetention, "reconcile", Mock())
    runner.workers.read.side_effect = StorageIOError("managed I/O failure")
    with pytest.raises(StorageIOError):
        await runner.start()
    assert not runner._tasks
