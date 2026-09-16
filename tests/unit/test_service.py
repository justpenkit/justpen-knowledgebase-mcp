"""Facade validation, absolute budgets and owned lifecycle with collaborators isolated."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from opentelemetry.context import Context, attach, detach

from justpen_knowledgebase_mcp import service
from justpen_knowledgebase_mcp.config import ServerConfig, WorkspacePolicy
from justpen_knowledgebase_mcp.errors import InvalidParamsError, LimitError
from justpen_knowledgebase_mcp.models import RetentionPolicyView, RetentionStatus
from justpen_knowledgebase_mcp.storage.maintenance import StatusCache
from justpen_knowledgebase_mcp.telemetry.context import extract_carrier

from .helpers import EVIDENCE, NODE


def facade():
    workers = Mock(read=AsyncMock(), write=AsyncMock())
    runner = Mock(ingest=AsyncMock(), read=AsyncMock(), delete=AsyncMock(), control=AsyncMock())
    runner.io = AsyncMock(side_effect=lambda _lane, callback, _deadline: callback())
    maintenance, sampler = Mock(), Mock()
    kb = service.KnowledgeBase(
        ServerConfig(workspace_dir=Path("/workspace")), Mock(), workers, maintenance, runner, sampler
    )
    return kb, workers, runner, maintenance, sampler


@pytest.mark.parametrize(
    "method",
    ["ingest_evidence", "read_evidence", "delete", "jobs", "write", "get", "neighbors", "search", "types", "reindex"],
)
async def test_facade_rejects_unknown_fields_before_work(method):
    kb, workers, _runner, _maintenance, _sampler = facade()
    with pytest.raises(InvalidParamsError):
        await getattr(kb, method)({"unexpected": "secret"})
    workers.read.assert_not_awaited()
    workers.write.assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "payload", "operation", "lane"),
    [
        ("write", {"nodes": [{"type": "domain", "properties": {"name": "example.com"}}]}, "write", "write"),
        ("get", {"kind": "nodes", "ids": [NODE]}, "get", "read"),
        ("neighbors", {"seed_ids": [NODE]}, "neighbors", "read"),
        ("search", {"kind": "nodes"}, "search", "read"),
        ("types", {"kind": "nodes"}, "graph_types", "read"),
        ("reindex", {"kind": "nodes", "all": True}, "admit_reindex", "write"),
    ],
)
async def test_facade_maps_valid_request_and_one_deadline(monkeypatch, method, payload, operation, lane):
    kb, workers, runner, _maintenance, _sampler = facade()
    seen = []

    async def dispatch(callback, token=None):
        seen.append(token)
        return callback(Mock(), token)

    getattr(workers, lane).side_effect = dispatch
    callback = Mock(return_value={"lane": "bulk"})
    monkeypatch.setattr(service.Graph if operation in ("write", "get") else service, operation, callback)
    assert await getattr(kb, method)(payload) == {"lane": "bulk"}
    assert len(seen) == 1
    assert seen[0].deadline > service.time.monotonic()
    if method == "reindex":
        runner.wake.assert_called_once_with("bulk")


@pytest.mark.parametrize(
    ("method", "payload", "target"),
    [
        ("ingest_evidence", {"text": "abc"}, "ingest"),
        ("read_evidence", {"evidence_id": EVIDENCE}, "read"),
        ("delete", {"kind": "nodes", "ids": [NODE]}, "delete"),
        ("jobs", {"action": "get", "job_id": NODE}, "control"),
    ],
)
async def test_job_facade_passes_validated_model_and_absolute_budget(method, payload, target):
    kb, _workers, runner, _maintenance, _sampler = facade()
    callback = getattr(runner, target)
    callback.return_value = {"status": "accepted"}
    assert await getattr(kb, method)(payload) == {"status": "accepted"}
    model, deadline = callback.call_args.args
    assert model.model_dump(exclude_unset=True) == payload
    if method == "ingest_evidence":
        assert deadline == runner.io.call_args.args[2]


async def test_search_budget_reports_incomplete():
    kb, workers, _runner, _maintenance, _sampler = facade()
    workers.read.side_effect = LimitError("internal")
    with pytest.raises(LimitError, match="search incomplete"):
        await kb.search({"kind": "nodes"})


@pytest.mark.parametrize("phase", ["normal", "reset", "unknown"])
async def test_status_uses_only_cached_collaborators(phase):
    kb, workers, runner, maintenance, sampler = facade()
    wal = StatusCache().snapshot()
    wal["phase"] = phase
    maintenance.status.return_value = wal
    sampler.snapshot.return_value = {}
    workers.queue_status.return_value = {
        "read_queued": 0,
        "write_queued": 0,
        "control_queued": 0,
        "reader_running": 0,
        "writer_running": 0,
        "stopping": False,
    }
    runner.queue_status.return_value = {"short": {"pending": 0}, "bulk": {"pending": 0}, "stopping": False}
    policy = RetentionPolicyView.model_validate(
        WorkspacePolicy().model_dump(include=set(RetentionPolicyView.model_fields))
    )
    runner.retention_status.return_value = RetentionStatus(policy=policy).model_dump()
    result = await kb.status()
    assert result["allowed_hosts"] == []
    assert result["wal"]["reason"] == (
        None if phase == "normal" else "RESET_PENDING" if phase == "reset" else "WAL_PRESSURE"
    )
    workers.read.assert_not_awaited()


async def test_lifespan_closes_owned_resources_in_order_even_after_failure(monkeypatch):
    calls = []
    workspace = Mock(close=Mock(side_effect=lambda: calls.append("workspace")))
    factory = Mock(close=Mock(side_effect=lambda: calls.append("factory")))
    workers = Mock(
        start=AsyncMock(),
        control=AsyncMock(return_value=WorkspacePolicy()),
        close=AsyncMock(side_effect=lambda: calls.append("workers")),
    )
    maintenance = Mock(start=AsyncMock(), close=AsyncMock(side_effect=lambda: calls.append("maintenance")))
    runner = Mock(start=AsyncMock(), close=AsyncMock(side_effect=lambda: calls.append("runner")))
    sampler = Mock(start=AsyncMock(), close=AsyncMock(side_effect=lambda: calls.append("sampler")))
    for name, value in [
        ("WorkspacePaths", workspace),
        ("SQLiteRuntime", factory),
        ("DatabaseWorkers", workers),
        ("CheckpointMaintenance", maintenance),
        ("JobRunner", runner),
        ("StatusSampler", sampler),
    ]:
        monkeypatch.setattr(service, name, Mock(return_value=value))
    observer = Mock()

    async def body():
        async with service.KnowledgeBase.open(
            ServerConfig(workspace_dir=Path("/workspace")), _shutdown_observer=observer
        ) as kb:
            assert kb.job_runner is runner
            raise ValueError("body")

    with pytest.raises(ValueError, match="body"):
        await body()
    assert calls == ["sampler", "runner", "maintenance", "workers", "factory", "workspace"]
    observer.start.assert_called_once()


async def test_cancelled_close_waits_for_owner_cleanup():
    started, release = asyncio.Event(), asyncio.Event()

    async def close():
        started.set()
        await release.wait()

    workers = Mock(close=AsyncMock(side_effect=close))
    task = asyncio.create_task(service._close_workers(workers, Mock(), Mock(close=AsyncMock())))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    workers.close.assert_awaited_once()


async def test_reindex_captures_context_before_worker_thread(monkeypatch):

    kb, workers, _runner, _maintenance, _sampler = facade()
    parent = "00-" + "11" * 16 + "-" + "22" * 8 + "-01"
    saved = []

    def admit(_connection, _request, _job_id, *, initiating_context):
        saved.append(initiating_context)
        return {"lane": "bulk"}

    monkeypatch.setattr(service, "admit_reindex", admit)

    async def worker(callback, operation):
        token = attach(Context())
        try:
            return callback(Mock(), operation)
        finally:
            detach(token)

    workers.write.side_effect = worker
    token = attach(extract_carrier({"traceparent": parent}).context)
    try:
        await kb.reindex({"kind": "nodes", "all": True})
    finally:
        detach(token)
    assert saved == [{"traceparent": parent}]
