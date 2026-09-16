"""Retention/recovery selection and ownership; no real files or durable store."""

import json
from unittest.mock import Mock

import pytest

from justpen_knowledgebase_mcp.config import WorkspacePolicy
from justpen_knowledgebase_mcp.errors import ConflictError, StorageIOError
from justpen_knowledgebase_mcp.storage import job_recovery, job_retention
from justpen_knowledgebase_mcp.storage.evidence import stage_name

from .helpers import NODE, OTHER, cursor, database, job


def test_retention_batch_prunes_expires_marks_files_and_skips_protection(monkeypatch):
    rows = [
        (1, NODE, "completed", 0),
        (2, OTHER, "failed", 0),
        (3, "protected", "failed", 0),
        (4, "fresh", "completed", 100),
    ]
    row_lookup = Mock(
        side_effect=[
            job(state="completed", lease_expires_at=None),
            job(
                uuid=OTHER,
                state="failed",
                lease_expires_at=None,
                payload=json.dumps({"input_token": NODE, "input_stage": stage_name(OTHER, NODE)}),
            ),
            job(uuid="protected", state="failed", lease_expires_at=None),
        ]
    )
    monkeypatch.setattr(job_retention, "job_row", row_lookup)
    monkeypatch.setattr(job_retention, "protected", Mock(side_effect=[False, False, True]))
    db = database(
        cursor(value='{"completed":2,"failed_cancelled":2}'),
        cursor(rows=[]),
        cursor(rows=rows),
        cursor(),
        cursor(),
        cursor(),
        cursor(),
    )
    policy = WorkspacePolicy(completed_retention_seconds=10, failed_cancelled_retention_seconds=10)
    result = job_retention.JobRetention.batch(db, policy, (0.0, 0), now=100)
    assert result == {"cursor": (0.0, 0), "examined": 4, "pruned": 1, "marked": 1, "invalid": 0}
    assert json.loads(db.execute.call_args.args[1][0]) == [NODE]
    assert row_lookup.call_count == 3


def test_recorded_tokens_validate_owned_names_and_deduplicate():
    row = job(
        payload=json.dumps({"input_token": OTHER, "input_stage": stage_name(NODE, OTHER)}),
        progress=json.dumps({"stage_token": OTHER}),
    )
    assert job_retention._recorded_tokens(row) == [OTHER]
    row["progress"] = json.dumps({"stage_token": NODE})
    assert job_retention._recorded_tokens(row) == [OTHER, NODE]
    row["payload"] = json.dumps({"input_token": OTHER, "input_stage": "foreign.stage"})
    with pytest.raises(StorageIOError):
        job_retention._recorded_tokens(row)


def test_purge_acknowledgment_head_and_protection(monkeypatch):
    row = job(
        state="failed",
        lease_expires_at=None,
        purge_pending=1,
        purge_tokens=json.dumps([OTHER]),
        progress=json.dumps({"stage_token": OTHER}),
    )
    monkeypatch.setattr(job_retention, "job_row", Mock(return_value=row))
    protection = Mock(return_value=False)
    monkeypatch.setattr(job_retention, "protected", protection)
    db = database()
    assert job_retention.JobRetention.next_file(db, NODE) == OTHER
    with pytest.raises(ConflictError, match="PURGE_TOKEN_CHANGED"):
        job_retention.JobRetention.acknowledge(db, NODE, NODE)
    db.execute.assert_not_called()
    job_retention.JobRetention.acknowledge(db, NODE, OTHER)
    assert db.execute.call_args.args[1] == (NODE,)
    protection.return_value = True
    with pytest.raises(ConflictError, match="JOB_RETENTION_PROTECTED"):
        job_retention.JobRetention.next_file(db, NODE)


def test_finalize_and_forget_require_completed_file_ownership(monkeypatch):
    row = job(
        state="completed",
        lease_expires_at=None,
        payload=json.dumps({"input_token": OTHER}),
        result='{"evidence_id":"ready"}',
    )
    monkeypatch.setattr(job_retention, "job_row", Mock(return_value=row))
    head = Mock(return_value=OTHER)
    monkeypatch.setattr(job_retention.JobRetention, "next_file", head)
    db = database()
    db.execute.return_value.get = 1
    assert not job_retention.JobRetention.finalize(db, NODE)
    head.return_value = None
    assert job_retention.JobRetention.finalize(db, NODE)
    db.reset_mock()
    job_retention.JobRetention.forget_clean_input(db, NODE, OTHER)
    assert db.execute.call_count == 2
    row["purge_pending"] = 1
    db.reset_mock()
    job_retention.JobRetention.forget_clean_input(db, NODE, OTHER)
    assert db.execute.call_count == 1
    db.execute.return_value.get = None
    assert not job_retention.JobRetention.finalize(db, NODE)


def test_retention_snapshot_reconciles_state_groups():
    db = database()
    db.execute.return_value.get = 2
    job_retention.JobRetention.reconcile(db)
    assert json.loads(db.execute.call_args.args[1][0]) == {"completed": 2, "failed_cancelled": 4}
    db = database(
        cursor(value=1), cursor(value='{"completed":3,"failed_cancelled":2}'), cursor(value=2), cursor(value=9)
    )
    assert job_retention.JobRetention.snapshot(db) == {
        "terminal_counts": {"completed": 3, "failed_cancelled": 2},
        "protected_count": 1,
        "needs_attention": True,
        "pending_prune_count": 2,
        "pruned_total": 9,
    }
    assert job_retention.JobRetention.next_job(database(cursor(rows=[(2, NODE)]))) == (2, NODE)
    assert job_retention.JobRetention.next_job(database(cursor())) is None


def test_recovery_reuses_immutable_job_id_and_cascade(monkeypatch):
    insert = Mock()
    monkeypatch.setattr(job_recovery.JobStore, "insert", insert)
    db = database(cursor(rows=[(1, NODE, OTHER, 1, 10)]), cursor(), cursor(rows=[(1, NODE, OTHER, 1, 10)]))
    assert job_recovery.recover_intents(db, "nodes", 0) == {"after_id": 0, "repaired": 1}
    assert insert.call_args.args[1:] == (OTHER, "delete", "short", {"kind": "nodes", "ids": [NODE], "cascade": True})


@pytest.mark.parametrize(
    ("payload", "result", "state", "token", "expiry", "disposable"),
    [
        ({"input_token": OTHER}, {}, "queued", None, None, False),
        ({"input_token": OTHER}, {"evidence_id": "ready"}, "completed", None, None, True),
        ({}, {}, "running", OTHER, float("inf"), False),
        ({}, {}, "running", OTHER, 0, True),
        ({}, {}, "failed", OTHER, None, True),
    ],
)
def test_staging_disposal_retains_live_or_unpublished_input(payload, result, state, token, expiry, disposable):
    db = database(cursor(rows=[(json.dumps(payload), json.dumps(result), state, token, expiry)]))
    assert job_recovery.staging_disposable(db, NODE, OTHER) is disposable


def test_stage_scan_bounded_and_ignores_unknown_names(monkeypatch):
    entries = [Mock(name="entry") for _ in range(34)]
    for entry in entries:
        entry.name = "unknown"
    entries[0].name = stage_name(NODE, OTHER)
    entries[1].name = "bad.token.stage"
    scan = Mock()
    scan.__enter__ = Mock(return_value=iter(entries))
    scan.__exit__ = Mock()
    monkeypatch.setattr(job_recovery.os, "scandir", Mock(return_value=scan))
    scanner = job_recovery.StageScan(Mock())
    assert scanner.batch() == [stage_name(NODE, OTHER)]
    assert scanner.iterator is not None
    assert scanner.batch() == []
    assert scanner.iterator is None
    scan.__exit__.assert_called_once()


@pytest.mark.parametrize(
    "progress",
    [
        "[]",
        "invalid",
        '{"verified_sha256":null}',
        '{"verified_sha256":"bad"}',
        '{"verified_sha256":"a","verified_sha256":"b"}',
    ],
)
def test_recorded_blob_rejects_unknown_metadata(progress):
    with pytest.raises(StorageIOError):
        job_retention.recorded_blob(job(progress=progress))


@pytest.mark.parametrize(("canonical", "peer", "expected"), [(1, None, False), (None, None, True), (None, 1, False)])
def test_orphan_proof_preserves_canonical_and_retryable_peer_owners(monkeypatch, canonical, peer, expected):
    monkeypatch.setattr(job_retention.JobRetention, "next_blob", Mock(return_value="a" * 64))
    db = database(cursor(value=canonical), cursor(value=peer))
    assert job_retention.JobRetention.blob_disposable(db, NODE, "a" * 64) is expected


def test_orphan_ack_and_finalization_require_unchanged_locator(monkeypatch):
    row = job(
        state="failed",
        lease_expires_at=None,
        purge_pending=1,
        purge_tokens="[]",
        blob_sha256="a" * 64,
        progress='{"verified_sha256":"' + "a" * 64 + '"}',
    )
    monkeypatch.setattr(job_retention, "job_row", Mock(return_value=row))
    monkeypatch.setattr(job_retention, "protected", Mock(return_value=False))
    db = database()
    db.execute.return_value.get = 1
    assert job_retention.JobRetention.next_blob(db, NODE) == "a" * 64
    assert not job_retention.JobRetention.finalize(db, NODE)
    with pytest.raises(ConflictError, match="PURGE_BLOB_CHANGED"):
        job_retention.JobRetention.acknowledge_blob(db, NODE, "b" * 64)
    job_retention.JobRetention.acknowledge_blob(db, NODE, "a" * 64)
    assert "json_remove(progress" in db.execute.call_args.args[0]
    row["purge_pending"] = 0
    with pytest.raises(ConflictError, match="PROTECTED"):
        job_retention.JobRetention.next_blob(db, NODE)


@pytest.mark.parametrize("tokens", ["invalid", "{}", '["invalid"]'])
def test_corrupt_purge_tokens_never_become_file_authority(monkeypatch, tokens):
    monkeypatch.setattr(
        job_retention,
        "job_row",
        Mock(return_value=job(state="failed", lease_expires_at=None, purge_pending=1, purge_tokens=tokens)),
    )
    monkeypatch.setattr(job_retention, "protected", Mock(return_value=False))
    with pytest.raises(StorageIOError, match="purge ownership"):
        job_retention.JobRetention.next_file(database(), NODE)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_ownership_rejects_nonfinite_persisted_json(constant):
    progress = '{"verified_sha256":"' + "a" * 64 + '","bytes":' + constant + "}"
    with pytest.raises(StorageIOError, match="malformed"):
        job_retention.recorded_blob(job(progress=progress))
