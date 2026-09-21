"""Reindex orchestration with isolated job/item fences and stream ownership."""

import json
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from justpen_knowledgebase_mcp import reindex
from justpen_knowledgebase_mcp.errors import (
    ConflictError,
    IndexingError,
    InvalidParamsError,
    NotFoundError,
    WalBusyError,
)
from justpen_knowledgebase_mcp.storage.fulltext import IndexOwner
from justpen_knowledgebase_mcp.text import TextChunk

from .helpers import EVIDENCE, NODE, OTHER, claim, cursor, database, job, owner


def runner_for(db):
    async def dispatch(callback):
        return callback(db, Mock())

    return Mock(
        workers=Mock(
            write=AsyncMock(side_effect=dispatch),
            read=AsyncMock(side_effect=dispatch),
            control=AsyncMock(side_effect=dispatch),
        )
    )


@pytest.mark.parametrize("media", ["text/plain", "application/octet-stream"])
async def test_index_item_cleans_then_finishes_coverage(monkeypatch, media):
    db = database()
    runner = runner_for(db)
    capability = claim()
    index_owner = IndexOwner(1, EVIDENCE, 2, 1, OTHER, "utf-8", media, 8)
    monkeypatch.setattr(reindex.JobStore, "fence", Mock(return_value=job()))
    monkeypatch.setattr(reindex, "claim_item", Mock(return_value=index_owner))
    monkeypatch.setattr(reindex, "clear_item_batch", Mock(side_effect=[False, True]))
    monkeypatch.setattr(reindex, "_index_stream", AsyncMock(return_value=(True, 2)))
    finish = Mock()
    monkeypatch.setattr(reindex, "finish_item", finish)
    result = await reindex.index_evidence(runner, capability, EVIDENCE)
    assert result["index_state"] == ("ready" if media == "text/plain" else "not_applicable")
    assert result["progress"] == {"bytes": 8, "chunks": 2 if media == "text/plain" else 0}
    assert result["warnings"] == (["TOKEN_TOO_LONG"] if media == "text/plain" else [])
    assert finish.call_args.kwargs["incomplete"] == (media == "text/plain")


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (IndexingError("TEXT_DECODE_FAILED"), "index_failed"),
        (ConflictError("JOB_CANCELLED"), "pending"),
        (OSError("io"), "index_failed"),
    ],
)
async def test_index_failure_preserves_generation_fence(monkeypatch, error, state):
    runner = runner_for(database())
    monkeypatch.setattr(reindex.JobStore, "fence", Mock(return_value=job()))
    monkeypatch.setattr(
        reindex, "claim_item", Mock(return_value=IndexOwner(1, EVIDENCE, 2, 1, OTHER, "utf-8", "text/plain", 8))
    )
    monkeypatch.setattr(reindex, "clear_item_batch", Mock(return_value=True))
    monkeypatch.setattr(reindex, "_index_stream", AsyncMock(side_effect=error))
    finish = Mock()
    monkeypatch.setattr(reindex, "finish_item", finish)
    with pytest.raises(type(error)):
        await reindex.index_evidence(runner, claim(), EVIDENCE)
    assert finish.call_args.args[2] == state
    assert finish.call_args.kwargs == {"incomplete": True}


def test_full_admission_reuses_matching_and_rejects_different_selection(monkeypatch):
    request = reindex.ReindexRequest(kind="nodes", all=True)
    monkeypatch.setattr(reindex.JobStore, "get", Mock(return_value={"lane": "bulk"}))
    db = database(cursor(rows=[(NODE, json.dumps(request.model_dump(exclude_none=True)), 0)]))
    assert reindex.admit_reindex(db, request, OTHER)["reused"]
    db = database(cursor(rows=[(NODE, '{"kind":"evidence","all":true}', 0)]))
    with pytest.raises(ConflictError, match="FULL_REINDEX_ACTIVE"):
        reindex.admit_reindex(db, request, OTHER)
    insert = Mock()
    monkeypatch.setattr(reindex.JobStore, "insert", insert)
    db = database(cursor(), cursor())
    assert reindex.admit_reindex(db, request, OTHER)["status"] == "accepted"
    insert.assert_called_once()
    assert "query_epoch=query_epoch+1" in db.execute.call_args.args[0]


def test_single_evidence_override_generation_and_media_validation(monkeypatch):
    row = owner(uuid=EVIDENCE, media_type="text/plain", encoding="utf-8")
    monkeypatch.setattr(reindex, "row_by_id", Mock(return_value=row))
    monkeypatch.setattr(reindex.JobStore, "insert", Mock())
    monkeypatch.setattr(reindex.JobStore, "get", Mock(return_value={"lane": "bulk"}))
    db = database()
    reindex.admit_reindex(
        db, reindex.ReindexRequest(kind="evidence", ids=[EVIDENCE], media_type="application/octet-stream"), NODE
    )
    assert db.execute.call_args.args[1][:2] == ("application/octet-stream", "auto")
    assert db.execute.call_args.args[1][4:6] == ("not_applicable", 0)
    row["media_type"] = "application/octet-stream"
    with pytest.raises(InvalidParamsError):
        reindex.admit_reindex(db, reindex.ReindexRequest(kind="evidence", ids=[EVIDENCE], encoding="utf-8"), NODE)


def test_next_record_explicit_end_missing_and_bulk(monkeypatch):
    monkeypatch.setattr(reindex.JobStore, "fence", Mock(return_value=job()))
    monkeypatch.setattr(reindex, "row_by_id", Mock(return_value=owner()))
    capability = claim(payload={"kind": "nodes", "ids": [NODE]})
    assert reindex._next_record(database(), capability) == (1, NODE)
    capability.progress["item_index"] = 1
    assert reindex._next_record(database(), capability) is None
    capability.progress.clear()
    monkeypatch.setattr(reindex, "row_by_id", Mock(return_value=None))
    with pytest.raises(NotFoundError):
        reindex._next_record(database(), capability)
    capability.payload = {"kind": "nodes", "all": True}
    assert reindex._next_record(database(cursor(rows=[(2, OTHER)])), capability) == (2, OTHER)


async def test_graph_reindex_step_refreshes_then_checkpoints_progress(monkeypatch):
    db = database()
    runner = runner_for(db)
    monkeypatch.setattr(reindex, "_next_record", Mock(return_value=(2, NODE)))
    monkeypatch.setattr(reindex.JobStore, "fence", Mock(return_value=job()))
    monkeypatch.setattr(reindex, "row_by_id", Mock(return_value=owner()))
    properties, text, release = Mock(), Mock(), Mock()
    monkeypatch.setattr(reindex, "refresh_properties", properties)
    monkeypatch.setattr(reindex, "refresh_record_text", text)
    monkeypatch.setattr(reindex.JobStore, "release", release)
    capability = claim(payload={"kind": "nodes"}, progress={"item_index": 4})
    await reindex.reindex_step(runner, capability)
    properties.assert_called_once()
    text.assert_called_once()
    assert release.call_args.args[2] == {"after_id": 2, "item_index": 5}
    monkeypatch.setattr(reindex, "_next_record", Mock(return_value=None))
    finish = Mock()
    monkeypatch.setattr(reindex.JobStore, "finish", finish)
    await reindex.reindex_step(runner, capability)
    assert finish.call_args.args[2] == "completed"


@pytest.mark.parametrize("reason", ["INDEX_BUSY", "INDEX_GENERATION_CHANGED", "RECORD_DELETING", "JOB_CANCELLED"])
async def test_full_reindex_bounded_partial_failure_samples(monkeypatch, reason):
    monkeypatch.setattr(reindex, "index_evidence", AsyncMock(side_effect=ConflictError(reason)))
    capability = claim(payload={"all": True, "kind": "evidence"})
    result = {}
    if reason == "JOB_CANCELLED":
        with pytest.raises(ConflictError):
            await reindex._reindex_evidence_item(Mock(), capability, EVIDENCE, result)
    else:
        for _ in range(33):
            await reindex._reindex_evidence_item(Mock(), capability, EVIDENCE, result)
        assert len(result["sample_ids"]) == 32
        assert result["sample_truncated"]
        assert result["coverage_incomplete"]


def test_publish_batch_counts_non_gap_and_checkpoints_once_without_refencing(monkeypatch):
    append, fence = Mock(), Mock()
    monkeypatch.setattr(reindex, "append_chunk", append)
    monkeypatch.setattr(reindex.JobStore, "fence", fence)
    db = database()
    batch = [
        TextChunk("alpha", "utf-8", 0, 1, 0),
        TextChunk("", "utf-8", 5, 1, 5, gap=True),
        TextChunk("omega", "utf-8", 5, 1, 5),
    ]
    assert reindex._publish_batch(db, claim(progress={"bytes": 8}), job(), Mock(), batch, 3) == 5
    assert append.call_count == 3
    # The caller already fenced this transaction; the checkpoint must not repeat it.
    fence.assert_not_called()
    assert db.execute.call_count == 1
    statement, parameters = db.execute.call_args.args
    assert statement.startswith("UPDATE jobs SET progress=?")
    assert json.loads(parameters[0]) == {"bytes": 8, "chunks": 5}


def test_publish_batch_preserves_an_outstanding_blob_locator(monkeypatch):
    # Bypassing JobStore.checkpoint must keep its ownership rules. Ingest clears
    # the locator before indexing today, so this pins the equivalence, not a live path.
    monkeypatch.setattr(reindex, "append_chunk", Mock())
    digest = "b" * 64
    row = job(progress=json.dumps({"bytes": 8, "chunks": 1, "verified_sha256": digest, "stage_token": OTHER}))
    row["blob_sha256"] = digest
    db = database()
    reindex._publish_batch(db, claim(progress={"chunks": 1}), row, Mock(), [TextChunk("alpha", "utf-8", 0, 1, 0)], 1)
    parameters = db.execute.call_args.args[1]
    assert json.loads(parameters[0]) == {"bytes": 8, "chunks": 2, "verified_sha256": digest, "stage_token": OTHER}
    assert parameters[1] == digest


def test_next_batch_bounds_one_transaction_by_published_bytes():
    size = reindex.BATCH_BYTES // 4
    stream = iter([TextChunk("x" * size, "utf-8", index * size, 1, index * size) for index in range(9)])
    assert [len(reindex._next_batch(stream)) for _ in range(4)] == [4, 4, 1, 0]


@pytest.mark.parametrize("fail", [False, True])
async def test_index_stream_closes_file_generator_on_publish_failure(monkeypatch, fail):

    runner = runner_for(database())
    runner.store = Mock(workspace=Mock(evidence=Path("/workspace/evidence")))
    runner.store.blob_name.return_value = "aa/blob"
    runner.store.workspace.open_managed_file.return_value = nullcontext(9)
    monkeypatch.setattr(reindex.os, "dup", Mock(return_value=10))
    stream_context = Mock()
    stream_context.__enter__ = Mock(return_value=Mock())
    stream_context.__exit__ = Mock()
    monkeypatch.setattr(reindex.os, "fdopen", Mock(return_value=stream_context))
    chunks = [TextChunk("abc", "utf-8", 0, 1, 0), TextChunk("", "utf-8", 3, 1, 3, gap=True)]
    monkeypatch.setattr(reindex, "iter_chunks", Mock(return_value=iter(chunks)))
    monkeypatch.setattr(reindex.JobStore, "fence", Mock(return_value=job()))
    publish = Mock(side_effect=ConflictError("INDEX_GENERATION_CHANGED") if fail else None, return_value=1)
    monkeypatch.setattr(reindex, "_publish_batch", publish)

    async def io(_lane, callback):
        return callback()

    runner.io = AsyncMock(side_effect=io)
    runner.close_io_owner = AsyncMock(side_effect=io)
    index_owner = IndexOwner(1, EVIDENCE, 2, 1, OTHER, "utf-8", "text/plain", 3)
    if fail:
        with pytest.raises(ConflictError):
            await reindex._index_stream(runner, claim(), index_owner)
    else:
        assert await reindex._index_stream(runner, claim(), index_owner) == (True, 1)
        # Both chunks, gap included, reach one transaction that starts from count zero.
        assert [chunk.gap for chunk in publish.call_args.args[-2]] == [False, True]
        assert [call.args[-1] for call in publish.call_args_list] == [0]
    runner.close_io_owner.assert_awaited_once()
    stream_context.__exit__.assert_called_once()


def test_full_admission_does_not_compare_telemetry_as_selection(monkeypatch):
    request = reindex.ReindexRequest(kind="nodes", all=True)
    payload = {**request.model_dump(exclude_none=True), "_telemetry": {"traceparent": "old"}}
    monkeypatch.setattr(reindex.JobStore, "get", Mock(return_value={"lane": "bulk"}))
    db = database(cursor(rows=[(NODE, json.dumps(payload), 0)]))
    assert reindex.admit_reindex(db, request, OTHER, initiating_context={"traceparent": "new"})["reused"]


async def test_pressure_does_not_clear_item_ownership_or_mask_permanent_failure(monkeypatch):
    runner = runner_for(database())
    runner.workers.control = runner.workers.write
    monkeypatch.setattr(reindex.JobStore, "fence", Mock(return_value=job()))
    monkeypatch.setattr(
        reindex, "claim_item", Mock(return_value=IndexOwner(1, EVIDENCE, 2, 1, OTHER, "utf-8", "text/plain", 8))
    )
    monkeypatch.setattr(reindex, "clear_item_batch", Mock(return_value=True))
    finish = Mock()
    monkeypatch.setattr(reindex, "finish_item", finish)
    monkeypatch.setattr(reindex, "_index_stream", AsyncMock(side_effect=WalBusyError("WAL_PRESSURE", 1000)))
    with pytest.raises(WalBusyError):
        await reindex.index_evidence(runner, claim(), EVIDENCE)
    finish.assert_not_called()
    monkeypatch.setattr(reindex, "_index_stream", AsyncMock(side_effect=IndexingError("TEXT_DECODE_FAILED")))
    runner.workers.control = AsyncMock(side_effect=WalBusyError("RESET_PENDING", 1000))
    with pytest.raises(IndexingError, match="TEXT_DECODE_FAILED"):
        await reindex.index_evidence(runner, claim(), EVIDENCE)
