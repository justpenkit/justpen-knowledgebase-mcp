"""Real retention and publication boundaries fail closed for row-local corruption."""

import json
from uuid import uuid4

import pytest

from justpen_knowledgebase_mcp.errors import StorageIOError
from justpen_knowledgebase_mcp.jobs import JobRunner
from justpen_knowledgebase_mcp.storage.evidence_records import EvidenceRecords
from justpen_knowledgebase_mcp.storage.jobs import JobStore, job_row

from .test_job_retention import terminal

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize(
    ("progress", "trusted"),
    [
        ({"verified_sha256": "a" * 64, "bytes": -1}, "a" * 64),
        ({"verified_sha256": "a" * 64, "chunks": []}, "a" * 64),
        ({"rows_deleted": "private"}, None),
        ({"verified_sha256": "b" * 64, "bytes": 1}, "a" * 64),
        ({"verified_sha256": "b" * 64, "bytes": 1}, None),
        ({"bytes": 1}, "a" * 64),
    ],
)
async def test_corrupt_owner_survives_selection_and_pending_purge(kb, pending, progress, trusted):
    await kb.job_runner.close()
    runner = JobRunner(kb.workers, kb.workspace, kb.job_runner.store.policy)
    staged = runner.store.stage_inline(b"trusted", str(uuid4()), str(uuid4()))
    runner.store.publish(staged)
    path = kb.workspace.evidence / runner.store.blob_name(staged.sha256)
    progress = dict(progress)
    if progress.get("verified_sha256") == "a" * 64:
        progress["verified_sha256"] = staged.sha256
    if trusted is not None:
        trusted = staged.sha256
    stored = json.dumps(progress)
    try:

        def populate(connection, _token):
            corrupt = terminal(connection, "failed")
            clean = terminal(connection)
            connection.execute(
                "UPDATE jobs SET progress=?,blob_sha256=?,purge_pending=? WHERE uuid=?",
                (stored, trusted, int(pending), corrupt),
            )
            return corrupt, clean

        corrupt, clean = await kb.workers.control(populate)
        await runner.retention_pass(force=True)
        if pending:
            with pytest.raises(StorageIOError):
                await runner._purge_step(corrupt)
        else:
            assert runner.retention_status()["needs_attention"]
        row = await kb.workers.read(lambda c, _t: job_row(c, corrupt))
        assert (row["progress"], row["blob_sha256"], row["purge_pending"]) == (stored, trusted, int(pending))
        assert path.read_bytes() == b"trusted"
        result = await kb.jobs({"action": "get", "job_id": corrupt})
        assert result["needs_attention"]
        assert "private" not in json.dumps(result)
        assert await kb.workers.read(lambda c, _t: c.execute("SELECT 1 FROM jobs WHERE uuid=?", (clean,)).get) is None
    finally:
        await runner.close()


@pytest.mark.parametrize("trusted", ["a" * 64, None])
async def test_existing_verified_blob_cannot_release_different_or_missing_locator(kb, trusted, monkeypatch):
    await kb.job_runner.close()
    runner = JobRunner(kb.workers, kb.workspace, kb.job_runner.store.policy)
    identifier = str(uuid4())
    try:
        staged = runner.store.stage_inline(b"\x00", identifier, str(uuid4()))
        runner.store.publish(staged)
        progress = {"verified_sha256": staged.sha256, "bytes": 1}

        def populate(connection, _token):
            JobStore.insert(
                connection,
                identifier,
                "ingest",
                "short",
                {
                    "media_type": "application/octet-stream",
                    "encoding": "auto",
                    "warnings": [],
                    "targets": [],
                },
            )
            connection.execute(
                "UPDATE jobs SET progress=?,blob_sha256=? WHERE uuid=?",
                (json.dumps(progress), staged.sha256, identifier),
            )
            claim = JobStore.claim(connection, "short", "ingest")
            connection.execute("UPDATE jobs SET blob_sha256=? WHERE uuid=?", (trusted, identifier))
            return claim

        claim = await kb.workers.control(populate)
        assert claim is not None
        assert runner.store.verify_blob(staged.sha256, 1, claim.check) is not None

        async def forbidden_copy(_claim):
            pytest.fail("existing blob reuse must not enter copy/checkpoint")

        monkeypatch.setattr(runner, "_copy_input", forbidden_copy)
        await runner._run_claim(claim)
        row = await kb.workers.read(lambda c, _t: job_row(c, identifier))
        assert row["blob_sha256"] == trusted
        assert row["state"] == "failed"
        assert json.loads(row["progress"]) == progress
        result = await kb.jobs({"action": "get", "job_id": identifier})
        assert result["needs_attention"]
        assert result["error"] == "IO_ERROR"
        assert len(result["reason"]) <= 1024
        assert await kb.workers.read(lambda c, _t: c.execute("SELECT count(*) FROM evidence").get) == 0
        assert (kb.workspace.evidence / runner.store.blob_name(staged.sha256)).read_bytes() == b"\x00"
    finally:
        await runner.close()


@pytest.mark.parametrize(
    ("trusted", "progress", "admitted"),
    [
        ("a" * 64, {"verified_sha256": "b" * 64}, "b" * 64),
        (None, {"verified_sha256": "b" * 64}, "b" * 64),
        ("a" * 64, {"verified_sha256": "a" * 64}, "b" * 64),
        (None, {}, "b" * 64),
        ("a" * 64, {"verified_sha256": "a" * 64, "bytes": -1}, "a" * 64),
    ],
)
async def test_fenced_publication_independently_checks_handoff(kb, trusted, progress, admitted):
    await kb.job_runner.close()
    identifier = str(uuid4())

    def populate(connection, _token):
        JobStore.insert(
            connection,
            identifier,
            "ingest",
            "short",
            {
                "media_type": "application/octet-stream",
                "encoding": "auto",
                "warnings": [],
                "targets": [],
            },
        )
        claim = JobStore.claim(connection, "short", "ingest")
        connection.execute(
            "UPDATE jobs SET progress=?,blob_sha256=? WHERE uuid=?", (json.dumps(progress), trusted, identifier)
        )
        return claim

    claim = await kb.workers.control(populate)
    with pytest.raises(StorageIOError):
        await kb.workers.write(lambda c, _t: EvidenceRecords.publish_record(c, claim, admitted, 1))
    row = await kb.workers.read(lambda c, _t: job_row(c, identifier))
    assert (row["blob_sha256"], json.loads(row["progress"]), row["state"]) == (trusted, progress, "running")
    assert await kb.workers.read(lambda c, _t: c.execute("SELECT count(*) FROM evidence").get) == 0
