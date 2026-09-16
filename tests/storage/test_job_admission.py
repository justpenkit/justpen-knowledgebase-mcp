"""Corrupt durable jobs are isolated without starving real worker lanes."""

import asyncio
import json
import time
from unittest.mock import Mock
from uuid import uuid4

import pytest

from justpen_knowledgebase_mcp.errors import ConflictError
from justpen_knowledgebase_mcp.jobs import JobRunner
from justpen_knowledgebase_mcp.models import DeleteRequest
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.job_recovery import staging_disposable
from justpen_knowledgebase_mcp.storage.job_retention import JobRetention
from justpen_knowledgebase_mcp.storage.jobs import JobStore, job_row

from .test_job_retention import terminal

pytestmark = pytest.mark.integration

CORRUPTION = [
    ("progress", raw)
    for raw in (
        "[]",
        "null",
        "42",
        '"private-secret"',
        "nope",
        '{"bytes":-1}',
        '{"bytes":true}',
        '{"chunks":1.5}',
        '{"bytes":NaN}',
        '{"bytes":1,"bytes":2}',
        '{"verified_sha256":"' + "b" * 64 + '"}',
    )
] + [(cell, raw) for cell in ("payload", "result") for raw in ("[]", "null", "42", '"private-secret"', "nope")]


def corrupt(connection, identifier, cell, raw):
    statements = {
        "payload": "UPDATE jobs SET payload=? WHERE uuid=?",
        "progress": "UPDATE jobs SET progress=? WHERE uuid=?",
        "result": "UPDATE jobs SET result=? WHERE uuid=?",
    }
    connection.execute(statements[cell], (raw, identifier))


@pytest.mark.parametrize(
    ("full_reindex", "cell", "raw"),
    [
        (full, cell, raw)
        for full in (False, True)
        for cell, raw in CORRUPTION
        if not (full and cell == "payload" and raw == "nope")
    ],
)
async def test_retry_rejects_corruption_before_any_mutation(kb, cell, raw, full_reindex):
    await kb.job_runner.close()
    identifier = str(uuid4())

    def populate(connection, _token):
        JobStore.insert(connection, identifier, "reindex" if full_reindex else "ingest", "bulk", {"all": True})
        claim = JobStore.claim(connection, "bulk", "reindex" if full_reindex else "ingest")
        assert claim is not None
        JobStore.finish(connection, claim, "failed", {})
        corrupt(connection, identifier, cell, raw)
        return job_row(connection, identifier), connection.execute(
            "SELECT query_epoch,terminal_job_counts FROM settings"
        ).get

    before = await kb.workers.control(populate)
    with pytest.raises(ConflictError, match="JOB_METADATA_INVALID"):
        await kb.jobs({"action": "retry", "job_id": identifier})
    after = await kb.workers.read(
        lambda c, _t: (job_row(c, identifier), c.execute("SELECT query_epoch,terminal_job_counts FROM settings").get)
    )
    assert after == before


@pytest.mark.parametrize(("cell", "raw"), CORRUPTION)
@pytest.mark.parametrize("expired", [False, True])
async def test_claim_isolates_raw_cells_once_and_public_get_list_are_safe(kb, cell, raw, expired):
    await kb.job_runner.close()
    identifier = str(uuid4())

    def populate(connection, _token):
        JobStore.insert(connection, identifier, "ingest", "short", {})
        if expired:
            JobStore.claim(connection, "short", "ingest", now=1)
        corrupt(connection, identifier, cell, raw)
        return job_row(connection, identifier)

    before = await kb.workers.control(populate)
    assert await kb.workers.control(lambda c, _t: JobStore.claim(c, "short", "ingest", now=100)) is None
    row = await kb.workers.read(lambda c, _t: job_row(c, identifier))
    assert row["state"] == "failed"
    assert row["error_code"] == "JOB_METADATA_INVALID"
    assert row["lease_token"] is row["lease_expires_at"] is None
    assert row["finished_at"] == row["updated_at"] == 100
    for key in ("payload", "progress", "result", "blob_sha256", "attempts", "cancel_requested"):
        assert row[key] == before[key]
    assert await kb.workers.control(lambda c, _t: JobStore.claim(c, "short", "ingest", now=200)) is None
    assert await kb.workers.read(lambda c, _t: JobRetention.counts(c)) == {"completed": 0, "failed_cancelled": 1}
    public = await kb.jobs({"action": "get", "job_id": identifier})
    listed = await kb.jobs({"action": "list"})
    assert public["needs_attention"]
    assert public["retention_protected"]
    assert public["error"] == "IO_ERROR"
    assert public["reason"] == "JOB_METADATA_INVALID"
    assert "private-secret" not in json.dumps([public, listed])


@pytest.mark.parametrize("batch", [False, True])
async def test_claim_bounds_corrupt_prefix_and_eventually_reaches_healthy(kb, batch):
    await kb.job_runner.close()
    healthy = str(uuid4())

    def populate(connection, _token):
        for _ in range(205):
            identifier = str(uuid4())
            JobStore.insert(connection, identifier, "ingest", "short", {})
            corrupt(connection, identifier, "progress", "[]")
        JobStore.insert(connection, healthy, "ingest", "short", {})

    await kb.workers.control(populate)
    after = 0
    for expected in (100, 200, 205):
        claim, after = await kb.workers.control(
            lambda c, _t, after=after: (
                JobStore.claim_batch(c, "short", "ingest", after)
                if batch
                else (JobStore.claim(c, "short", "ingest"), 0)
            )
        )
        assert (await kb.workers.read(lambda c, _t: JobRetention.counts(c)))["failed_cancelled"] == expected
        assert (claim.job_id if claim else None) == (healthy if expected == 205 else None)


@pytest.mark.parametrize("lane", ["short", "bulk"])
@pytest.mark.parametrize("failure", ["corrupt", "execution", "admission"])
async def test_real_lane_survives_and_runs_healthy_ingest(kb, monkeypatch, lane, failure, capsys):
    await kb.job_runner.close()
    runner = JobRunner(kb.workers, kb.workspace, kb.job_runner.store.policy)
    poisoned, healthy = str(uuid4()), str(uuid4())
    stage = runner.store.stage_inline(b"\x00healthy", healthy, str(uuid4()))

    def populate(connection, _token):
        JobStore.insert(connection, poisoned, "ingest", lane, {})
        if failure == "corrupt":
            corrupt(connection, poisoned, "payload", '["private-secret"]')
        JobStore.insert(
            connection,
            healthy,
            "ingest",
            lane,
            {
                "media_type": "application/octet-stream",
                "encoding": "auto",
                "targets": [],
                "warnings": [],
                "input_stage": stage.name,
                "input_token": stage.name.split(".")[1],
                "input_size": stage.byte_size,
                "input_sha256": stage.sha256,
            },
        )

    await kb.workers.control(populate)
    step_events = Mock(wraps=runner.events.job_step)
    monkeypatch.setattr(runner.events, "job_step", step_events)
    original_step = runner._ingest_step

    async def execute(claim):
        if claim.job_id == poisoned:
            raise AttributeError("private-secret")
        await original_step(claim)

    monkeypatch.setattr(runner, "_ingest_step", execute)
    original_category = runner._category_step
    seen = False

    async def category(selected_lane, selected_category):
        nonlocal seen
        if failure == "admission" and not seen:
            seen = True
            raise AttributeError("private-secret")
        return await original_category(selected_lane, selected_category)

    monkeypatch.setattr(runner, "_category_step", category)
    task = asyncio.create_task(runner._lane(lane))
    runner._tasks = [task]
    try:
        result = await runner.wait(healthy, time.monotonic() + 3)
        assert result["state"] == "completed"
        assert not task.done()
        if failure == "corrupt":
            assert step_events.call_count == 1  # Only healthy work reaches telemetry carrier handling.
        assert (await kb.jobs({"action": "get", "job_id": poisoned}))["state"] == "failed"
        assert "private-secret" not in json.dumps(result) + capsys.readouterr().err
        if lane == "short":
            await assert_healthy_cleanup(kb, runner, result["evidence_id"])
            assert not task.done()
    finally:
        await runner.close()


async def assert_healthy_cleanup(kb, runner, evidence_id):
    deleted = await runner.delete(DeleteRequest(kind="evidence", ids=[evidence_id]), time.monotonic() + 3)
    assert deleted["state"] == "completed"
    stale_id, stale_token = str(uuid4()), str(uuid4())
    stale_stage = runner.store.stage_inline(b"retained input", stale_id, stale_token)

    def old_owner(connection, _token):
        identifier = terminal(connection, payload={"input_token": stale_token})
        connection.execute(
            "UPDATE jobs SET uuid=?,payload=? WHERE uuid=?",
            (stale_id, json.dumps({"input_token": stale_token, "input_stage": stale_stage.name}), identifier),
        )

    await kb.workers.control(old_owner)
    await runner.retention_pass(force=True)
    for _ in range(300):
        if not await kb.workers.read(lambda c, _t: c.execute("SELECT 1 FROM jobs WHERE uuid=?", (stale_id,)).get):
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("healthy purge did not complete")
    assert not (kb.workspace.tmp / stale_stage.name).exists()


async def test_quarantine_preserves_trusted_blob_stage_and_repair_survives_second_service(kb):
    await kb.job_runner.close()
    runner = JobRunner(kb.workers, kb.workspace, kb.job_runner.store.policy)
    identifier, token = str(uuid4()), str(uuid4())
    staged = runner.store.stage_inline(b"owned", identifier, token)
    runner.store.publish(staged)
    retained = runner.store.stage_inline(b"input", identifier, token)

    def populate(connection, _token):
        JobStore.insert(connection, identifier, "ingest", "short", {"input_token": token, "input_stage": retained.name})
        connection.execute("UPDATE jobs SET progress='[]',blob_sha256=? WHERE uuid=?", (staged.sha256, identifier))
        JobStore.claim(connection, "short", "ingest", now=1)
        return job_row(connection, identifier)

    try:
        before = await kb.workers.control(populate)
        async with KnowledgeBase.open(kb.config) as second:
            assert await second.workers.control(lambda c, _t: JobStore.claim(c, "short", "ingest")) is None
            public = await second.jobs({"action": "get", "job_id": identifier})
            assert public["retention_protected"]
            assert await second.workers.read(lambda c, _t: job_row(c, identifier)) == before
        await runner.retention_pass(force=True)
        assert runner.retention_status()["needs_attention"]
        assert not await kb.workers.control(lambda c, _t: staging_disposable(c, identifier, token))
        await runner._orphan_step(retained.name)
        assert (kb.workspace.tmp / retained.name).read_bytes() == b"input"
        assert (kb.workspace.evidence / runner.store.blob_name(staged.sha256)).read_bytes() == b"owned"
        assert await kb.workers.read(lambda c, _t: job_row(c, identifier)) == before
        await kb.workers.control(
            lambda c, _t: c.execute(
                "UPDATE jobs SET progress=? WHERE uuid=?",
                (json.dumps({"verified_sha256": staged.sha256, "bytes": 5}), identifier),
            ).fetchall()
        )
        repaired = await kb.jobs({"action": "retry", "job_id": identifier})
        assert repaired["state"] == "queued"
        assert not repaired["needs_attention"]
        assert (await kb.workers.read(lambda c, _t: JobRetention.counts(c)))["failed_cancelled"] == 0
    finally:
        await runner.close()


async def test_recent_quarantine_attention_follows_bounded_retention_sweep(kb):
    await kb.job_runner.close()
    runner = JobRunner(kb.workers, kb.workspace, kb.job_runner.store.policy)
    identifier = str(uuid4())
    current = time.time()

    def populate(connection, _token):
        for _ in range(205):
            terminal(connection, finished=current)
        JobStore.insert(connection, identifier, "ingest", "short", {})
        connection.execute("UPDATE jobs SET progress='[]',blob_sha256=? WHERE uuid=?", ("a" * 64, identifier))
        JobStore.claim(connection, "short", "ingest", now=current)
        return job_row(connection, identifier)

    try:
        before = await kb.workers.control(populate)
        public = await kb.jobs({"action": "get", "job_id": identifier})
        assert public["needs_attention"]
        assert public["retention_protected"]
        assert not (await kb.workers.read(lambda c, _t: JobRetention.snapshot(c)))["needs_attention"]
        assert await runner.retention_pass(force=True)
        assert not runner.retention_status()["needs_attention"]
        assert await runner.retention_pass(force=True)
        assert not runner.retention_status()["needs_attention"]
        assert not await runner.retention_pass(force=True)
        assert runner.retention_status()["needs_attention"]
        assert await kb.workers.read(lambda c, _t: job_row(c, identifier)) == before
    finally:
        await runner.close()
