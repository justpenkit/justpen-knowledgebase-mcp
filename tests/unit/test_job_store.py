"""Durable-job decisions using read snapshots and recorded transaction commands."""

import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from justpen_knowledgebase_mcp.errors import ConflictError, InvalidParamsError, NotFoundError, StorageIOError
from justpen_knowledgebase_mcp.storage import evidence_records, jobs
from justpen_knowledgebase_mcp.storage.deletions import DeleteStep

from .helpers import EVIDENCE, NODE, OTHER, claim, cursor, database, job, owner


def test_claim_empty_and_reclaim_fresh_capability(monkeypatch):
    assert jobs.JobStore.claim(database(cursor()), "short", "ingest", now=10) is None
    row = job(
        payload='{"source":"secret"}', progress='{"bytes":8}', result='{"evidence_id":"existing"}', cancel_requested=1
    )
    db = database(cursor(value=NODE), cursor(), cursor(record=row))
    selected = jobs.JobStore.claim(db, "short", "ingest", now=10)
    assert selected is not None
    assert selected.job_id == NODE
    assert selected.token != OTHER
    assert selected.expires_at == 40
    assert selected.cancelled
    assert selected.progress == {"bytes": 8}
    assert selected.result == {"evidence_id": "existing"}
    assert db.execute.call_args_list[1].args[1][0] == selected.token
    with pytest.raises(NotFoundError):
        jobs.job_row(database(cursor()), NODE)


@pytest.mark.parametrize(
    "patch", [{"state": "queued"}, {"lease_token": NODE}, {"lease_expires_at": 10}, {"purge_pending": 1}]
)
def test_fence_rejects_stale_capability_without_mutation(patch):
    db = database(cursor(record=job(**patch)))
    with pytest.raises(ConflictError, match="CLAIM_LOST"):
        jobs.JobStore.fence(db, claim(), now=10)
    assert db.execute.call_count == 1


@pytest.mark.parametrize(
    ("patch", "message"),
    [({"lost": True}, "CLAIM_LOST"), ({"expires_at": 0}, "CLAIM_LOST"), ({"cancelled": True}, "JOB_CANCELLED")],
)
def test_local_claim_checks(patch, message):
    with pytest.raises(ConflictError, match=message):
        claim(**patch).check()
    claim().check()


def test_accept_rejects_inline_body_and_binds_payload():
    db = database()
    for key in ("text", "base64"):
        with pytest.raises(InvalidParamsError):
            jobs.JobStore.insert(db, NODE, "ingest", "short", {key: "private"})
    db.execute.assert_not_called()
    jobs.JobStore.insert(db, NODE, "ingest", "short", {"source": "a' OR 1"})
    assert json.loads(db.execute.call_args.args[1][-1]) == {"source": "a' OR 1"}
    assert "a' OR 1" not in db.execute.call_args.args[0]


def test_heartbeat_checkpoint_finish_and_release_preserve_fence(monkeypatch):
    fence = Mock(return_value=job(cancel_requested=1))
    monkeypatch.setattr(jobs.JobStore, "fence", fence)
    monkeypatch.setattr(jobs.time, "time", lambda: 20)
    db = database()
    capability = claim()
    assert jobs.JobStore.heartbeat(db, capability) == (50, True)
    db.reset_mock()
    with pytest.raises(ConflictError, match="JOB_CANCELLED"):
        jobs.JobStore.checkpoint(db, capability, {"bytes": 5})
    db.execute.assert_not_called()
    fence.return_value = job()
    jobs.JobStore.checkpoint(db, capability, {"bytes": 5})
    assert json.loads(db.execute.call_args.args[1][0]) == {"bytes": 5}
    jobs.JobStore.release(db, capability, {"chunks": 2})
    assert json.loads(db.execute.call_args.args[1][0]) == {"chunks": 2}
    for state in ("completed", "failed", "cancelled"):
        db.reset_mock()
        jobs.JobStore.finish(db, capability, state, {"error": "SAFE"}, now=20)
        assert db.execute.call_count == 2
        assert db.execute.call_args_list[0].args[1] == (state, '{"error": "SAFE"}', 20, 20, "SAFE", NODE)
        assert db.execute.call_args_list[1].args[1][-1] == 1
    with pytest.raises(InvalidParamsError):
        jobs.JobStore.finish(db, capability, "running", {})


def test_failure_keeps_evidence_coverage(monkeypatch):
    monkeypatch.setattr(jobs.JobStore, "fence", Mock(return_value=job()))
    finish = Mock()
    monkeypatch.setattr(jobs.JobStore, "finish", finish)
    monkeypatch.setattr(
        jobs, "row_by_id", Mock(return_value=owner(media_type="text/plain", index_state="index_failed", incomplete=1))
    )
    capability = claim(kind="reindex", payload={"kind": "evidence", "ids": [EVIDENCE]}, result={"bytes": 8})
    jobs.JobStore.finish_failure(database(), capability, "failed", {"error": "IO_ERROR"})
    result = finish.call_args.args[3]
    assert result["evidence_id"] == EVIDENCE
    assert result["incomplete"] is True
    assert result["bytes"] == 8
    assert result["error"] == "IO_ERROR"


@pytest.mark.parametrize(
    ("state", "kind", "payload", "updates"),
    [
        ("queued", "ingest", {}, 2),
        ("queued", "reindex", {"all": True}, 1),
        ("running", "ingest", {}, 1),
        ("completed", "ingest", {}, 0),
    ],
)
def test_cancel_transition_counts(monkeypatch, state, kind, payload, updates):
    monkeypatch.setattr(jobs, "job_row", Mock(return_value=job(state=state, kind=kind, payload=json.dumps(payload))))
    monkeypatch.setattr(jobs.JobStore, "get", Mock(return_value={"state": "accepted"}))
    db = database()
    assert jobs.JobStore.cancel(db, NODE) == {"state": "accepted"}
    assert db.execute.call_count == updates


@pytest.mark.parametrize("patch", [{"kind": "delete"}, {"purge_pending": 1}])
def test_cancel_immutable_intent_and_purge(monkeypatch, patch):
    monkeypatch.setattr(jobs, "job_row", Mock(return_value=job(**patch)))
    db = database()
    with pytest.raises(ConflictError):
        jobs.JobStore.cancel(db, NODE)
    db.execute.assert_not_called()


@pytest.mark.parametrize(
    ("patch", "error"), [({"purge_pending": 1}, ConflictError), ({"state": "running"}, InvalidParamsError)]
)
def test_retry_invalid_state(monkeypatch, patch, error):
    monkeypatch.setattr(jobs, "job_row", Mock(return_value=job(**patch)))
    with pytest.raises(error):
        jobs.JobStore.retry(database(), NODE)


def test_retry_full_reindex_owns_epoch_and_counters(monkeypatch):
    monkeypatch.setattr(jobs, "job_row", Mock(return_value=job(state="failed", kind="reindex", payload='{"all":true}')))
    monkeypatch.setattr(jobs.JobStore, "get", Mock(return_value={"state": "queued"}))
    db = database(cursor(value=1))
    with pytest.raises(ConflictError, match="FULL_REINDEX_ACTIVE"):
        jobs.JobStore.retry(db, NODE)
    db = database()
    db.execute.return_value.get = None
    assert jobs.JobStore.retry(db, NODE)["state"] == "queued"
    assert db.execute.call_count == 5
    assert db.execute.call_args_list[-2].args[1][-1] == -1
    assert "query_epoch=query_epoch+1" in db.execute.call_args_list[1].args[0]


def test_public_get_filters_internal_progress_and_retention(monkeypatch):
    row = job(
        state="completed",
        lease_expires_at=None,
        finished_at=1,
        payload='{"media_type":"text/plain","input_stage":"secret"}',
        progress='{"bytes":8,"stage_token":"secret"}',
    )
    monkeypatch.setattr(jobs, "job_row", Mock(return_value=row))
    monkeypatch.setattr(jobs, "protected", Mock(return_value=False))
    db = database(cursor(value='{"completed_retention_seconds":10}'))
    result = jobs.JobStore.get(db, NODE)
    assert result["progress"] == {"bytes": 8, "chunks": 0, "rows_deleted": 0}
    assert result["index_state"] == "pending"
    assert result["incomplete"] is True
    assert not result["retention_protected"]
    assert result["expires_at"]
    assert "secret" not in json.dumps(result)


def test_failed_job_get_reuses_one_pending_owner_observation(monkeypatch):
    monkeypatch.setattr(jobs, "job_row", Mock(return_value=job(state="failed", lease_expires_at=None)))
    protection = Mock(return_value=False)
    monkeypatch.setattr(jobs, "protected", protection)
    value = jobs.JobStore.get(database(cursor(value='{"failed_cancelled_retention_seconds":10}')), NODE)
    assert not value["retention_protected"]
    assert not value["needs_attention"]
    protection.assert_called_once_with(protection.call_args.args[0], NODE)


@pytest.mark.parametrize(
    ("rows", "step", "expected"),
    [
        ([], None, "finish"),
        ([(1, NODE, OTHER, 1, 10)], DeleteStep(4, done=True), "finish"),
        ([(1, NODE, OTHER, 1, 10)], DeleteStep(4, done=False), "release"),
        ([(1, NODE, OTHER, 1, 10)], DeleteStep(4, done=False, files_pending=True), "files"),
    ],
)
def test_delete_step_progress_and_publication_boundary(monkeypatch, rows, step, expected):
    monkeypatch.setattr(jobs.JobStore, "fence", Mock(return_value=job()))
    monkeypatch.setattr(jobs.GraphDeletion, "step", Mock(return_value=step))
    finish, release = Mock(), Mock()
    monkeypatch.setattr(jobs.JobStore, "finish", finish)
    monkeypatch.setattr(jobs.JobStore, "release", release)
    monkeypatch.setattr(jobs, "row_by_id", Mock(return_value=owner(sha256="a" * 64)))
    result = jobs.JobStore.delete_step(
        database(cursor(rows=rows)),
        claim(kind="delete", payload={"kind": "evidence", "ids": [NODE]}, progress={"rows_deleted": 2}),
    )
    if expected == "finish":
        finish.assert_called_once()
        release.assert_not_called()
    else:
        assert release.call_args.args[2]["rows_deleted"] == 6
        assert result == ({"evidence_id": NODE, "sha256": "a" * 64} if expected == "files" else None)


@pytest.mark.parametrize("remaining", [False, True])
def test_finalize_evidence_after_unlink(monkeypatch, remaining):
    monkeypatch.setattr(jobs.JobStore, "fence", Mock(return_value=job()))
    monkeypatch.setattr(jobs, "row_by_id", Mock(return_value=owner(lifecycle="delete_pending", delete_job_id=NODE)))
    finish, release = Mock(), Mock()
    monkeypatch.setattr(jobs.JobStore, "finish", finish)
    monkeypatch.setattr(jobs.JobStore, "release", release)
    db = database(cursor(), cursor(rows=[(2,)] if remaining else []))
    jobs.JobStore.finalize_evidence(db, claim(payload={"ids": [EVIDENCE]}), EVIDENCE)
    assert finish.call_count == int(not remaining)
    assert release.call_count == int(remaining)
    assert db.execute.call_args_list[0].args[1] == (1,)


def test_rotating_claim_wraps_once_and_skips_unexpired_lease(monkeypatch):
    chosen = claim()
    select = Mock(return_value=chosen)
    monkeypatch.setattr(jobs.JobStore, "_claim_selected", select)
    db = database(cursor(rows=[]), cursor(rows=[(1, NODE, "running", 99), (2, OTHER, "queued", None)]))
    assert jobs.JobStore.claim_batch(db, "bulk", "reindex", 50, now=10) == (chosen, 2)
    select.assert_called_once_with(db, OTHER, 10)
    assert db.execute.call_count == 2
    assert db.execute.call_args.args[1] == ("bulk", "reindex", 0, 50, 100)


def test_rotating_claim_stops_after_hundred_live_leases(monkeypatch):
    select = Mock()
    monkeypatch.setattr(jobs.JobStore, "_claim_selected", select)
    db = database(cursor(rows=[(index, NODE, "running", 99) for index in range(1, 101)]))
    assert jobs.JobStore.claim_batch(db, "bulk", "reindex", 0, now=10) == (None, 100)
    select.assert_not_called()
    assert db.execute.call_count == 1


@pytest.mark.parametrize(
    ("state", "active", "warnings"),
    [("index_failed", False, []), ("pending", True, ["INDEX_REPAIR_QUEUED"]), ("ready", False, [])],
)
def test_finish_dedup_recomputes_repair_warning(monkeypatch, state, active, warnings):
    monkeypatch.setattr(jobs.JobStore, "fence", Mock(return_value=job()))
    monkeypatch.setattr(
        evidence_records,
        "row_by_id",
        Mock(
            return_value=owner(
                media_type="text/plain", index_state=state, incomplete=state != "ready", index_owner_job_id=2
            )
        ),
    )
    monkeypatch.setattr(evidence_records, "index_owner_active", Mock(return_value=active))
    finish = Mock()
    monkeypatch.setattr(jobs.JobStore, "finish", finish)
    evidence_records.EvidenceRecords.finish_dedup(
        database(), claim(result={"warnings": ["TARGET_NOT_FOUND", "INDEX_REPAIR_QUEUED"]}), EVIDENCE
    )
    assert finish.call_args.args[3]["warnings"] == ["TARGET_NOT_FOUND", *warnings]


@pytest.mark.parametrize("transition", ["checkpoint", "release"])
def test_verified_progress_omission_cannot_lose_stage_or_size(monkeypatch, transition):
    previous = {"verified_sha256": "a" * 64, "bytes": 17, "stage_token": OTHER}
    row = job(blob_sha256="a" * 64, progress=json.dumps(previous))
    monkeypatch.setattr(jobs.JobStore, "fence", Mock(return_value=row))
    db = database()
    getattr(jobs.JobStore, transition)(db, claim(), {"verified_sha256": "a" * 64, "chunks": 2})
    assert json.loads(db.execute.call_args.args[1][0]) == {**previous, "chunks": 2}


@pytest.mark.parametrize(
    ("current", "previous", "updated", "error"),
    [
        (None, "{}", {"verified_sha256": None}, StorageIOError),
        (None, "{}", {"verified_sha256": "A" * 64}, StorageIOError),
        ("a" * 64, '{"verified_sha256":"' + "a" * 64 + '"}', {"verified_sha256": "b" * 64}, ConflictError),
        ("a" * 64, "invalid", {"chunks": 1}, StorageIOError),
        ("a" * 64, "{}", {"chunks": 1}, StorageIOError),
    ],
)
def test_checkpoint_rejects_unknown_or_replaced_outstanding_ownership(monkeypatch, current, previous, updated, error):
    monkeypatch.setattr(jobs.JobStore, "fence", Mock(return_value=job(blob_sha256=current, progress=previous)))
    db = database()
    with pytest.raises(error):
        jobs.JobStore.checkpoint(db, claim(), updated)
    db.execute.assert_not_called()


@pytest.mark.parametrize(
    "progress", [{"bytes": -1}, {"chunks": []}, {"rows_deleted": "private"}, {"bytes": True}, {"chunks": 1.5}]
)
def test_invalid_progress_counters_return_attention_without_mutation(monkeypatch, progress):
    stored = json.dumps(progress)
    row = job(state="failed", lease_expires_at=None, progress=stored, blob_sha256="a" * 64)
    monkeypatch.setattr(jobs, "job_row", Mock(return_value=row))
    monkeypatch.setattr(jobs, "protected", Mock(return_value=False))
    result = jobs.JobStore.get(database(cursor(value='{"failed_cancelled_retention_seconds":10}')), NODE)
    assert result["needs_attention"]
    assert result["progress"] == {"bytes": 0, "chunks": 0, "rows_deleted": 0}
    assert "private" not in json.dumps(result)
    assert row["progress"] == stored
    assert row["blob_sha256"] == "a" * 64


@pytest.mark.parametrize("patch", [{"payload": '{"warnings":null}'}, {"result": '{"warnings":null}'}])
def test_invalid_progress_fallback_does_not_hide_unrelated_result_errors(monkeypatch, patch):
    row = job(progress='{"bytes":-1}', **patch)
    monkeypatch.setattr(jobs, "job_row", Mock(return_value=row))
    with pytest.raises(ValidationError):
        jobs.JobStore.get(database(cursor(value="{}")), NODE)
