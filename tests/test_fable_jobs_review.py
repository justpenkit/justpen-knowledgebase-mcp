"""Focused real-storage regressions identified during the Fable job audit."""

import asyncio
import json
import os
import threading
import time
from typing import cast
from uuid import uuid4

import pytest

from justpen_knowledgebase_mcp.errors import (
    BusyError,
    ConfigurationError,
    ConflictError,
    PathDeniedError,
    StorageIOError,
)
from justpen_knowledgebase_mcp.jobs import JobRunner
from justpen_knowledgebase_mcp.reindex import ReindexRequest, admit_reindex
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.evidence_records import EvidenceRecords
from justpen_knowledgebase_mcp.storage.fulltext import claim_item
from justpen_knowledgebase_mcp.storage.job_ownership import OTHER_BLOB_OWNER_SQL
from justpen_knowledgebase_mcp.storage.job_retention import PROTECTED_COUNT_SQL, JobRetention, recorded_blob
from justpen_knowledgebase_mcp.storage.jobs import CLAIM_BATCH_SQL, Claim, JobStore, job_row
from justpen_knowledgebase_mcp.storage.schema import SchemaGuard

pytestmark = pytest.mark.integration


async def test_malformed_retention_row_does_not_block_clean_row(kb):
    await kb.job_runner.close()

    def populate(connection, _token):
        identifiers = []
        for payload in ({"input_token": str(uuid4()), "input_stage": "mismatch.stage"}, {}):
            identifier = str(uuid4())
            JobStore.insert(connection, identifier, "ingest", "bulk", payload)
            claim = cast("Claim", JobStore.claim(connection, "bulk", "ingest", now=1))
            JobStore.finish(connection, claim, "failed", {}, now=2)
            identifiers.append(identifier)
        return identifiers

    corrupt, clean = await kb.workers.control(populate)
    await kb.workers.control(lambda c, _t: JobRetention.batch(c, kb.job_runner.store.policy, (0.0, 0)))
    remaining = await kb.workers.read(lambda c, _t: [row[0] for row in c.execute("SELECT uuid FROM jobs")])
    assert clean not in remaining
    assert corrupt in remaining


async def test_cancel_requested_full_reindex_is_not_reused(kb):
    await kb.job_runner.close()

    def accept_and_cancel(connection, _token):
        request = ReindexRequest(kind="nodes", all=True)
        accepted = admit_reindex(connection, request, str(uuid4()))
        JobStore.cancel(connection, accepted["job_id"])
        return admit_reindex(connection, request, str(uuid4()))

    with pytest.raises(ConflictError, match="FULL_REINDEX_ACTIVE"):
        await kb.workers.control(accept_and_cancel)


async def test_dedup_ingest_completes_without_competing_with_active_index_owner(kb):
    await kb.job_runner.close()

    def deduplicate(connection, _token):
        digest = "a" * 64
        evidence_id = "e_" + digest
        connection.execute(
            "INSERT INTO evidence(uuid,sha256,byte_size,media_type,encoding,blob_path,index_state,incomplete) "
            "VALUES(?,?,4,'text/plain','auto',?,'pending',1)",
            (evidence_id, digest, "aa/aa/" + digest),
        )
        owner_id = str(uuid4())
        JobStore.insert(connection, owner_id, "reindex", "bulk", {"kind": "evidence", "ids": [evidence_id]})
        owner = cast("Claim", JobStore.claim(connection, "bulk", "reindex"))
        internal_id = connection.execute("SELECT id FROM jobs WHERE uuid=?", (owner_id,)).get
        claim_item(connection, evidence_id, internal_id, owner.token)
        dedup_id = str(uuid4())
        JobStore.insert(
            connection,
            dedup_id,
            "ingest",
            "short",
            {
                "media_type": "text/plain",
                "encoding": "auto",
                "media_explicit": False,
                "encoding_explicit": False,
                "warnings": [],
                "targets": [],
                "source": "second",
            },
        )
        claim = cast("Claim", JobStore.claim(connection, "short", "ingest"))
        EvidenceRecords.publish_record(connection, claim, digest, 4)
        return (
            connection.execute("SELECT state FROM jobs WHERE uuid=?", (dedup_id,)).get,
            connection.execute("SELECT index_owner_job_id FROM evidence WHERE uuid=?", (evidence_id,)).get,
            internal_id,
        )

    state, retained_owner, original_owner = await kb.workers.control(deduplicate)
    assert retained_owner == original_owner
    assert state == "completed"


async def test_inline_admission_bytes_are_checked_before_attempt_copy(kb):
    job_id, admission_token, attempt_token = str(uuid4()), str(uuid4()), str(uuid4())
    staged = await kb.job_runner.io(
        "short", lambda: kb.job_runner.store.stage_inline(b"original", job_id, admission_token)
    )
    (kb.workspace.tmp / staged.name).write_bytes(b"TRUNC")
    claim = Claim(
        job_id,
        attempt_token,
        "ingest",
        "short",
        {
            "input_stage": staged.name,
            "input_token": admission_token,
            "input_size": staged.byte_size,
            "input_sha256": staged.sha256,
        },
        {},
        time.time() + 30,
    )
    with pytest.raises(StorageIOError):
        await kb.job_runner._copy_input(claim)


async def test_orphan_retry_remains_queued_after_transient_busy(kb, monkeypatch):
    runner = kb.job_runner
    name = f"{uuid4()}.{uuid4()}.stage"
    runner._orphans.append(name)

    async def unavailable(_name):
        raise BusyError("transient")

    monkeypatch.setattr(runner, "_orphan_step", unavailable)
    with pytest.raises(BusyError):
        await runner._cleanup_category("orphan")
    assert name in runner._orphans


async def test_retention_event_below_threshold_does_not_run_counts(kb, monkeypatch):
    await kb.job_runner.close()
    runner = kb.job_runner
    runner._retention_due = time.time() + 3600
    observed = []
    original = JobRetention.snapshot

    def snapshot(connection):
        connection.set_exec_trace(lambda _cursor, sql, _bindings: observed.append(sql) or True)
        try:
            return original(connection)
        finally:
            connection.set_exec_trace(None)

    monkeypatch.setattr(JobRetention, "snapshot", staticmethod(snapshot))
    await runner.retention_pass()
    assert not any("count(" in sql.lower() for sql in observed), json.dumps(observed)


async def test_released_reindex_yields_to_another_waiting_bulk_job(kb):
    await kb.job_runner.close()

    def select(connection, _token):
        first_id, second_id = str(uuid4()), str(uuid4())
        JobStore.insert(connection, first_id, "reindex", "bulk", {"kind": "nodes", "all": True})
        JobStore.insert(connection, second_id, "reindex", "bulk", {"kind": "nodes", "ids": [str(uuid4())]})
        selected, after = JobStore.claim_batch(connection, "bulk", "reindex", 0)
        first = cast("Claim", selected)
        JobStore.release(connection, first, {"after_id": 1})
        selected, _after = JobStore.claim_batch(connection, "bulk", "reindex", after)
        following = cast("Claim", selected)
        return following.job_id, second_id

    actual, waiting = await kb.workers.control(select)
    assert actual == waiting


@pytest.mark.parametrize("replacement", [None, "CREATE INDEX jobs_active_lane ON jobs(state)"])
async def test_required_active_index_missing_or_wrong_layout_rejects_reopen(kb, replacement):

    await kb.job_runner.close()

    def alter(connection, _token):
        connection.execute("DROP INDEX IF EXISTS jobs_active_lane")
        if replacement is not None:
            connection.execute(replacement)

    await kb.workers.control(alter)
    with pytest.raises(ConfigurationError):
        async with KnowledgeBase.open(kb.config):
            pass


async def test_active_claim_scan_is_bounded_and_indexed(kb):

    await kb.job_runner.close()

    def inspect(connection, _token):
        ids = []
        for index in range(101):
            identifier = str(uuid4())
            JobStore.insert(connection, identifier, "reindex", "bulk", {"kind": "nodes", "ids": [str(uuid4())]})
            if index < 100:
                connection.execute(
                    "UPDATE jobs SET state='running',lease_expires_at=? WHERE uuid=?", (time.time() + 100, identifier)
                )
            ids.append(identifier)
        first, after = JobStore.claim_batch(connection, "bulk", "reindex", 0)
        second, _after = JobStore.claim_batch(connection, "bulk", "reindex", after)
        plan = [
            row[3]
            for row in connection.execute(
                "EXPLAIN QUERY PLAN " + CLAIM_BATCH_SQL, ("bulk", "reindex", 0, 2**63 - 1, 100)
            )
        ]
        protected = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + PROTECTED_COUNT_SQL)]
        return first, after, second, ids[-1], plan, protected

    first, after, second, expected, plan, protected = await kb.workers.control(inspect)
    assert first is None
    assert after == 100
    assert second.job_id == expected
    assert any("jobs_active_lane" in item and "id>?" in item for item in plan), plan
    assert not any("TEMP B-TREE" in item for item in plan), plan
    assert not any("jobs_terminal" in item for item in protected), protected


@pytest.mark.parametrize(
    "changes",
    [
        {"progress": "invalid"},
        {"progress": "[]"},
        {"progress": '{"verified_sha256":"../bad"}'},
        {"payload": '{"input_token":"bad","input_stage":"bad.stage"}'},
    ],
)
async def test_malformed_retention_preserves_locators_and_advances(kb, changes):
    await kb.job_runner.close()

    def batch(connection, _token):
        identifiers = []
        for index in range(101):
            identifier = str(uuid4())
            JobStore.insert(connection, identifier, "ingest", "short", {})
            connection.execute("UPDATE jobs SET state='failed',finished_at=1 WHERE uuid=?", (identifier,))
            if index < 100:
                for column, value in changes.items():
                    if column == "payload":
                        connection.execute("UPDATE jobs SET payload=? WHERE uuid=?", (value, identifier))
                    else:
                        connection.execute("UPDATE jobs SET progress=? WHERE uuid=?", (value, identifier))
            identifiers.append(identifier)
        JobRetention.reconcile(connection)
        first = JobRetention.batch(connection, kb.job_runner.store.policy, (0.0, 0))
        second = JobRetention.batch(connection, kb.job_runner.store.policy, first["cursor"])
        repeated = JobRetention.batch(connection, kb.job_runner.store.policy, (0.0, 0))
        return first, second, repeated, connection.execute("SELECT count(*) FROM jobs").get

    first, second, repeated, remaining = await kb.workers.control(batch)
    assert first["invalid"] == 100
    assert second["pruned"] == 1
    assert repeated["invalid"] == 100
    assert remaining == 100


@pytest.mark.parametrize("action", ["purge", "retry"])
async def test_post_publish_cancel_does_not_lose_last_orphan_locator(kb, monkeypatch, action):
    await kb.job_runner.close()
    runner = JobRunner(kb.workers, kb.workspace, kb.job_runner.store.policy)
    job_id, admission_token = str(uuid4()), str(uuid4())
    try:
        staged = await runner.io("short", lambda: runner.store.stage_inline(b"orphan", job_id, admission_token))
        options = {
            "input_stage": staged.name,
            "input_token": admission_token,
            "input_size": staged.byte_size,
            "input_sha256": staged.sha256,
            "media_type": "application/octet-stream",
            "encoding": "auto",
            "media_explicit": False,
            "encoding_explicit": False,
            "warnings": [],
            "targets": [],
        }

        def accept(connection, _token):
            JobStore.insert(connection, job_id, "ingest", "short", options)
            return JobStore.claim(connection, "short", "ingest")

        claim = cast("Claim", await kb.workers.control(accept))
        publish = runner.store.publish

        def publish_then_cancel(attempt):
            publish(attempt)
            claim.cancelled = True

        monkeypatch.setattr(runner.store, "publish", publish_then_cancel)
        await runner._run_claim(claim)
        state, evidence_count = await kb.workers.read(
            lambda c, _t: (
                c.execute("SELECT state FROM jobs WHERE uuid=?", (job_id,)).get,
                c.execute("SELECT count(*) FROM evidence").get,
            )
        )
        assert state == "cancelled"
        assert evidence_count == 0
        blob_path = kb.workspace.evidence / runner.store.blob_name(staged.sha256)
        assert blob_path.exists()
        assert await kb.workers.read(lambda c, _t: job_row(c, job_id)["blob_sha256"]) == staged.sha256
        if action == "retry":
            monkeypatch.setattr(runner.store, "publish", publish)
            await kb.workers.control(lambda c, _t: JobStore.retry(c, job_id))
            retry = await kb.workers.control(lambda c, _t: JobStore.claim(c, "short", "ingest"))
            await runner._run_claim(retry)
            completed = await kb.workers.read(lambda c, _t: job_row(c, job_id))
            assert completed["state"] == "completed"
            assert completed["blob_sha256"] is None
            assert blob_path.exists()
            return
        await kb.workers.control(lambda c, _t: c.execute("UPDATE jobs SET finished_at=1 WHERE uuid=?", (job_id,)))
        await kb.workers.control(lambda c, _t: JobRetention.batch(c, runner.store.policy, (0.0, 0)))
        await runner._purge_step(job_id)
        await runner._purge_step(job_id)
        await runner._purge_step(job_id)
        retained = await kb.workers.read(lambda c, _t: c.execute("SELECT 1 FROM jobs WHERE uuid=?", (job_id,)).get)
        assert retained is None
        assert not blob_path.exists()
    finally:
        await runner.close()


@pytest.fixture
async def orphan_case(kb):
    await kb.job_runner.close()
    runner = JobRunner(kb.workers, kb.workspace, kb.job_runner.store.policy)
    job_id = str(uuid4())
    try:

        def accept(connection, _token):
            JobStore.insert(connection, job_id, "ingest", "short", {})
            return JobStore.claim(connection, "short", "ingest")

        claim = cast("Claim", await kb.workers.control(accept))
        staged = await runner.io("short", lambda: runner.store.stage_inline(b"orphan proof", job_id, claim.token))
        await kb.workers.control(
            lambda c, _t: JobStore.checkpoint(
                c, claim, {"verified_sha256": staged.sha256, "bytes": staged.byte_size, "stage_token": claim.token}
            )
        )
        bucket = await runner.acquire_bucket("short", staged.sha256, exclusive=True)
        try:
            await runner.io("short", lambda: runner.store.publish(staged))
        finally:
            os.close(bucket)
        await kb.workers.control(lambda c, _t: JobStore.finish(c, claim, "failed", {}))
        await kb.workers.control(lambda c, _t: c.execute("UPDATE jobs SET finished_at=1 WHERE uuid=?", (job_id,)))
        await kb.workers.control(lambda c, _t: JobRetention.batch(c, runner.store.policy, (0.0, 0)))
        await runner._purge_step(job_id)
        yield runner, job_id, staged.sha256, kb.workspace.evidence / runner.store.blob_name(staged.sha256)
    finally:
        await runner.close()


@pytest.mark.parametrize("lifecycle", ["ready", "delete_pending"])
async def test_orphan_cleanup_preserves_canonical_ready_and_pending(kb, orphan_case, lifecycle):
    runner, job_id, digest, path = orphan_case

    def canonical(connection, _token):
        connection.execute(
            "INSERT INTO evidence(uuid,sha256,byte_size,media_type,encoding,blob_path) VALUES(?,?,12,'application/octet-stream','auto',?)",
            ("e_" + digest, digest, runner.store.blob_name(digest)),
        )
        if lifecycle == "delete_pending":
            connection.execute(
                "UPDATE evidence SET lifecycle='delete_pending',delete_job_id=?,delete_cascade=1,delete_requested_at=1 WHERE sha256=?",
                (str(uuid4()), digest),
            )

    await kb.workers.control(canonical)
    await runner._purge_step(job_id)
    assert path.exists()
    assert await kb.workers.read(lambda c, _t: c.execute("SELECT 1 FROM jobs WHERE uuid=?", (job_id,)).get) is None
    assert (
        await kb.workers.read(lambda c, _t: c.execute("SELECT lifecycle FROM evidence WHERE sha256=?", (digest,)).get)
        == lifecycle
    )


@pytest.mark.parametrize("state", ["queued", "running", "failed"])
async def test_orphan_cleanup_protects_other_consumers_and_retryable_peers(kb, orphan_case, state):
    runner, job_id, digest, path = orphan_case
    peer_id = str(uuid4())

    def peer(connection, _token):
        JobStore.insert(connection, peer_id, "ingest", "bulk", {})
        connection.execute(
            "UPDATE jobs SET state=?,progress=?,blob_sha256=?,lease_expires_at=?,finished_at=? WHERE uuid=?",
            (
                state,
                json.dumps({"verified_sha256": digest, "bytes": 12}),
                digest,
                time.time() + 30 if state == "running" else None,
                time.time() if state == "failed" else None,
                peer_id,
            ),
        )
        JobRetention.reconcile(connection)

    await kb.workers.control(peer)
    await runner._purge_step(job_id)
    assert path.exists()
    assert await kb.workers.read(lambda c, _t: c.execute("SELECT 1 FROM jobs WHERE uuid=?", (job_id,)).get) is None
    assert await kb.workers.read(lambda c, _t: recorded_blob(job_row(c, peer_id))) == digest
    if state == "failed":
        await kb.workers.control(lambda c, _t: JobStore.retry(c, peer_id))
        await runner._purge_step(job_id)
        assert path.exists()


@pytest.mark.parametrize(
    "progress",
    [
        "invalid",
        "[]",
        '{"verified_sha256":1}',
        '{"verified_sha256":null}',
        '{"verified_sha256":"Z"}',
        '{"verified_sha256":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}',
    ],
)
async def test_unrelated_corrupt_peer_does_not_block_blob_cleanup(kb, orphan_case, progress):
    runner, job_id, _digest, path = orphan_case

    def peer(connection, _token):
        identifier = str(uuid4())
        JobStore.insert(connection, identifier, "ingest", "bulk", {})
        connection.execute("UPDATE jobs SET progress=? WHERE uuid=?", (progress, identifier))

    await kb.workers.control(peer)
    await runner._purge_step(job_id)
    assert not path.exists()
    assert await kb.workers.read(lambda c, _t: c.execute("SELECT 1 FROM jobs WHERE uuid=?", (job_id,)).get) is None


async def test_all_peers_purge_fenced_allow_idempotent_orphan_cleanup(kb, orphan_case):
    runner, job_id, digest, path = orphan_case
    peer_id = str(uuid4())

    def peer(connection, _token):
        JobStore.insert(connection, peer_id, "ingest", "bulk", {})
        connection.execute(
            "UPDATE jobs SET state='failed',finished_at=1,progress=?,blob_sha256=?,purge_pending=1 WHERE uuid=?",
            (json.dumps({"verified_sha256": digest, "bytes": 12}), digest, peer_id),
        )
        JobRetention.reconcile(connection)

    await kb.workers.control(peer)
    with pytest.raises(ConflictError, match="JOB_PURGING"):
        await kb.workers.control(lambda c, _t: JobStore.retry(c, peer_id))
    await runner._purge_step(job_id)
    assert path.exists()  # Purge-pending peer is now the last durable owner.
    await runner._purge_step(peer_id)
    await runner._purge_step(job_id)
    assert not path.exists()
    assert await kb.workers.read(lambda c, _t: c.execute("SELECT count(*) FROM jobs").get) == 0


@pytest.mark.parametrize("phase", ["before_unlink", "after_unlink", "before_ack"])
async def test_orphan_cleanup_failure_retains_locator_until_durable_ack(kb, orphan_case, monkeypatch, phase):
    runner, job_id, digest, path = orphan_case
    unlink = runner.store.unlink_orphan_blob
    acknowledge = JobRetention.acknowledge_blob

    def fail_unlink(value):
        if phase == "after_unlink":
            unlink(value)
        raise OSError("simulated filesystem failure")

    def fail_ack(*_args):
        raise BusyError("simulated unavailable control")

    if phase == "before_ack":
        monkeypatch.setattr(JobRetention, "acknowledge_blob", staticmethod(fail_ack))
    else:
        monkeypatch.setattr(runner.store, "unlink_orphan_blob", fail_unlink)
    with pytest.raises((BusyError, OSError)):
        await runner._purge_step(job_id)
    assert await kb.workers.read(lambda c, _t: JobRetention.next_blob(c, job_id)) == digest
    assert path.exists() == (phase == "before_unlink")
    monkeypatch.setattr(runner.store, "unlink_orphan_blob", unlink)
    monkeypatch.setattr(JobRetention, "acknowledge_blob", staticmethod(acknowledge))
    await runner._purge_step(job_id)
    assert not path.exists()
    assert await kb.workers.read(lambda c, _t: c.execute("SELECT 1 FROM jobs WHERE uuid=?", (job_id,)).get) is None


@pytest.mark.parametrize(
    ("identifier", "progress"), [("not-uuid", {"verified_sha256": "a" * 64}), (str(uuid4()), {"verified_sha256": None})]
)
async def test_token_free_malformed_locator_is_retained(kb, identifier, progress):
    await kb.job_runner.close()

    def corrupt(connection, _token):
        JobStore.insert(connection, identifier, "ingest", "short", {})
        connection.execute(
            "UPDATE jobs SET state='failed',finished_at=1,progress=? WHERE uuid=?", (json.dumps(progress), identifier)
        )
        JobRetention.reconcile(connection)
        return JobRetention.batch(connection, kb.job_runner.store.policy, (0.0, 0))

    result = await kb.workers.control(corrupt)
    assert result["invalid"] == 1
    assert result["pruned"] == 0
    assert result["marked"] == 0


@pytest.mark.parametrize("variant", ["last", "first", "escaped", "null"])
async def test_duplicate_digest_keys_do_not_own_unrelated_blobs(kb, orphan_case, variant):

    runner, job_id, digest, path = orphan_case
    key = "verified\\u005fsha256" if variant == "escaped" else "verified_sha256"
    other = "null" if variant == "null" else json.dumps("b" * 64)
    first, second = (json.dumps(digest), other) if variant == "first" else (other, json.dumps(digest))
    progress = '{"verified_sha256":' + first + ',"' + key + '":' + second + "}"
    peer_id = str(uuid4())

    def peer(connection, _token):
        JobStore.insert(connection, peer_id, "ingest", "bulk", {})
        connection.execute("UPDATE jobs SET progress=? WHERE uuid=?", (progress, peer_id))

    await kb.workers.control(peer)
    await runner._purge_step(job_id)
    assert not path.exists()
    with pytest.raises(StorageIOError, match="malformed"):
        await kb.workers.read(lambda c, _t: recorded_blob(job_row(c, peer_id)))


async def test_blob_owner_lookup_uses_index_through_unrelated_terminal_history(kb, orphan_case):

    _runner, job_id, digest, _path = orphan_case

    def inspect(connection, _token):
        connection.executemany(
            "INSERT INTO jobs(uuid,kind,lane,state,requested_at,updated_at) VALUES(?,'ingest','short','completed',1,1)",
            [(str(uuid4()),) for _ in range(1000)],
        )
        plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + OTHER_BLOB_OWNER_SQL, (digest, job_id))]
        return plan, connection.execute(OTHER_BLOB_OWNER_SQL, (digest, job_id)).get

    plan, owner = await kb.workers.control(inspect)
    assert owner is None
    assert any("jobs_blob_locator" in item and "SEARCH" in item for item in plan), plan
    assert not any("SCAN jobs" in item for item in plan), plan


async def test_unlocked_verifier_locator_blocks_orphan_cleanup_before_publication(kb, orphan_case, monkeypatch):

    runner, job_id, digest, path = orphan_case
    peer_id = str(uuid4())

    def accept(connection, _token):
        JobStore.insert(
            connection,
            peer_id,
            "ingest",
            "bulk",
            {
                "media_type": "application/octet-stream",
                "encoding": "auto",
                "media_explicit": False,
                "encoding_explicit": False,
                "warnings": [],
                "targets": [],
            },
        )
        connection.execute(
            "UPDATE jobs SET progress=?,blob_sha256=? WHERE uuid=?",
            (json.dumps({"verified_sha256": digest, "bytes": 12}), digest, peer_id),
        )
        return JobStore.claim(connection, "bulk", "ingest")

    claim = await kb.workers.control(accept)
    entered, release = threading.Event(), threading.Event()
    original = runner.store.verify_blob

    def verify(digest, byte_size, check):
        def pause_hash():
            check()
            entered.set()
            assert release.wait(3)

        return original(digest, byte_size, pause_hash)

    monkeypatch.setattr(runner.store, "verify_blob", verify)
    task = asyncio.create_task(runner._ingest_step(claim))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        with runner.store.bucket(digest, exclusive=True, deadline=time.monotonic()):
            pass
        await runner._purge_step(job_id)
        assert path.exists()
        assert await kb.workers.read(lambda c, _t: recorded_blob(job_row(c, peer_id))) == digest
    finally:
        release.set()
        await task
    await runner._purge_step(job_id)
    assert path.exists()
    assert await kb.workers.read(lambda c, _t: c.execute("SELECT 1 FROM evidence WHERE sha256=?", (digest,)).get) == 1


@pytest.mark.parametrize("late_owner", [False, True])
async def test_dedup_repair_warning_and_late_owner_race(kb, late_owner):
    await kb.job_runner.close()
    runner = JobRunner(kb.workers, kb.workspace, kb.job_runner.store.policy)
    digest, dedup_id, owner_id = "c" * 64, str(uuid4()), str(uuid4())
    try:

        def publish(connection, _token):
            identifier = "e_" + digest
            connection.execute(
                "INSERT INTO evidence(uuid,sha256,byte_size,media_type,encoding,blob_path,index_state,incomplete) VALUES(?,?,3,'text/plain','auto',?,'index_failed',1)",
                (identifier, digest, runner.store.blob_name(digest)),
            )
            JobStore.insert(
                connection,
                dedup_id,
                "ingest",
                "short",
                {
                    "media_type": "text/plain",
                    "encoding": "auto",
                    "media_explicit": False,
                    "encoding_explicit": False,
                    "warnings": [],
                    "targets": [],
                },
            )
            claim = cast("Claim", JobStore.claim(connection, "short", "ingest"))
            result = EvidenceRecords.publish_record(connection, claim, digest, 3)
            if late_owner:
                JobStore.insert(connection, owner_id, "reindex", "bulk", {"kind": "evidence", "ids": [identifier]})
                owner = cast("Claim", JobStore.claim(connection, "bulk", "reindex"))
                number = connection.execute("SELECT id FROM jobs WHERE uuid=?", (owner_id,)).get
                claim_item(connection, identifier, number, owner.token)
            return result, JobStore.claim(connection, "short", "ingest")

        result, claim = await kb.workers.control(publish)
        assert result["warnings"] == ["INDEX_REPAIR_QUEUED"]
        if late_owner:
            await runner._ingest_step(claim)
            value = await kb.workers.read(lambda c, _t: JobStore.get(c, dedup_id))
            assert value["state"] == "completed"
            assert value["index_state"] == "pending"
            assert value["incomplete"]
            assert (
                await kb.workers.read(
                    lambda c, _t: (
                        c.execute(
                            "SELECT index_owner_job_id=(SELECT id FROM jobs WHERE uuid=?) FROM evidence WHERE sha256=?",
                            (owner_id, digest),
                        ).get
                    )
                )
                == 1
            )
    finally:
        await runner.close()


async def test_repeated_pressure_deferral_removes_only_settled_attempt_stages(kb, monkeypatch):
    await kb.job_runner.close()
    runner = JobRunner(kb.workers, kb.workspace, kb.job_runner.store.policy)
    runner._stop_event.set()
    identifier, input_token = str(uuid4()), str(uuid4())
    try:
        staged = await runner.io(
            "short", lambda: runner.store.stage_inline(b"persistent input", identifier, input_token)
        )
        options = {
            "input_stage": staged.name,
            "input_token": input_token,
            "input_size": staged.byte_size,
            "input_sha256": staged.sha256,
            "media_type": "application/octet-stream",
            "encoding": "auto",
            "media_explicit": False,
            "encoding_explicit": False,
            "warnings": [],
            "targets": [],
        }
        await kb.workers.control(lambda c, _t: JobStore.insert(c, identifier, "ingest", "short", options))
        checkpoint = JobStore.checkpoint

        def pressure(*_args):
            raise BusyError("after completed copy")

        monkeypatch.setattr(JobStore, "checkpoint", staticmethod(pressure))
        for _ in range(2):
            claim = await kb.workers.control(lambda c, _t: JobStore.claim(c, "short", "ingest"))
            await runner._run_claim(claim)
            assert not (kb.workspace.tmp / f"{identifier}.{claim.token}.stage").exists()
            assert (kb.workspace.tmp / staged.name).read_bytes() == b"persistent input"
        monkeypatch.setattr(JobStore, "checkpoint", staticmethod(checkpoint))
        claim = await kb.workers.control(lambda c, _t: JobStore.claim(c, "short", "ingest"))
        await runner._run_claim(claim)
        result = await kb.workers.read(lambda c, _t: JobStore.get(c, identifier))
        assert result["state"] == "completed"
        assert result["attempts"] == 3
        assert (kb.workspace.evidence / runner.store.blob_name(staged.sha256)).read_bytes() == b"persistent input"
    finally:
        await runner.close()


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
async def test_nonfinite_unrelated_peer_is_retained_without_blocking_orphan_disposal(kb, orphan_case, constant):
    runner, job_id, digest, path = orphan_case
    peer_id = str(uuid4())
    progress = '{"verified_sha256":"' + digest + '","bytes":' + constant + "}"

    def corrupt(connection, _token):
        JobStore.insert(connection, peer_id, "ingest", "short", {})
        connection.execute("UPDATE jobs SET state='failed',finished_at=1,progress=? WHERE uuid=?", (progress, peer_id))
        JobRetention.reconcile(connection)
        return JobRetention.batch(connection, runner.store.policy, (0.0, 0))

    batch = await kb.workers.control(corrupt)
    assert batch["invalid"] == 1
    await runner._purge_step(job_id)
    assert not path.exists()
    assert (
        await kb.workers.read(lambda c, _t: c.execute("SELECT purge_pending FROM jobs WHERE uuid=?", (peer_id,)).get)
        == 0
    )


@pytest.mark.parametrize("depth", [0, 1])
async def test_unpublished_verified_checkpoint_purges_absent_digest_path(kb, depth):
    await kb.job_runner.close()
    runner = JobRunner(kb.workers, kb.workspace, kb.job_runner.store.policy)
    job_id = str(uuid4())
    try:

        def accept(connection, _token):
            JobStore.insert(connection, job_id, "ingest", "short", {})
            return JobStore.claim(connection, "short", "ingest")

        claim = cast("Claim", await kb.workers.control(accept))
        staged = await runner.io("short", lambda: runner.store.stage_inline(b"never published", job_id, claim.token))
        await kb.workers.control(
            lambda c, _t: JobStore.checkpoint(
                c, claim, {"verified_sha256": staged.sha256, "bytes": staged.byte_size, "stage_token": claim.token}
            )
        )
        if depth:
            (kb.workspace.evidence / staged.sha256[:2]).mkdir()
        await kb.workers.control(lambda c, _t: JobStore.finish(c, claim, "failed", {}))
        await kb.workers.control(lambda c, _t: c.execute("UPDATE jobs SET finished_at=1 WHERE uuid=?", (job_id,)))
        await kb.workers.control(lambda c, _t: JobRetention.batch(c, runner.store.policy, (0.0, 0)))
        await runner._purge_step(job_id)
        assert not (kb.workspace.tmp / staged.name).exists()
        await runner._purge_step(job_id)
        await runner._purge_step(job_id)
        assert await kb.workers.read(lambda c, _t: c.execute("SELECT 1 FROM jobs WHERE uuid=?", (job_id,)).get) is None
        assert not (kb.workspace.evidence / runner.store.blob_name(staged.sha256)).exists()
    finally:
        await runner.close()


@pytest.mark.parametrize("change", ["absent_root", "replaced_root", "symlink_root", "symlink_digest"])
async def test_orphan_unlink_rejects_changed_root_and_symlink_descendants(kb, orphan_case, change):
    runner, job_id, digest, path = orphan_case
    original = kb.workspace.evidence if change != "symlink_digest" else path.parent.parent
    moved = original.with_name(original.name + "-saved")
    original.rename(moved)
    try:
        if change == "replaced_root":
            original.mkdir()
        elif change in {"symlink_root", "symlink_digest"}:
            original.symlink_to(moved, target_is_directory=True)
        with pytest.raises((OSError, StorageIOError, PathDeniedError)):
            await runner._purge_step(job_id)
    finally:
        if original.is_symlink():
            original.unlink()
        elif original.exists():
            original.rmdir()
        moved.rename(original)
    assert path.read_bytes() == b"orphan proof"
    assert await kb.workers.read(lambda c, _t: recorded_blob(job_row(c, job_id))) == digest


async def test_orphan_absence_sync_failure_retains_locator_for_retry(kb, orphan_case, monkeypatch):
    runner, job_id, digest, path = orphan_case
    path.unlink()
    sync = os.fsync
    with monkeypatch.context() as patch:

        def fail_sync(_fd):
            raise OSError("injected absence sync failure")

        patch.setattr(os, "fsync", fail_sync)
        with pytest.raises(OSError, match="absence sync failure"):
            await runner._purge_step(job_id)
    assert os.fsync is sync
    assert await kb.workers.read(lambda c, _t: recorded_blob(job_row(c, job_id))) == digest
    await runner._purge_step(job_id)
    assert await kb.workers.read(lambda c, _t: c.execute("SELECT 1 FROM jobs WHERE uuid=?", (job_id,)).get) is None


@pytest.mark.parametrize("layout", ["missing_column", "missing_index", "wrong_index"])
async def test_blob_ownership_layout_requires_offline_upgrade(kb, layout):
    await kb.job_runner.close()

    def alter(connection, _token):
        connection.execute("DROP INDEX jobs_blob_locator")
        if layout == "missing_column":
            connection.execute("ALTER TABLE jobs DROP COLUMN blob_sha256")
        elif layout == "wrong_index":
            connection.execute("CREATE INDEX jobs_blob_locator ON jobs(blob_sha256) WHERE purge_pending=0")
        check = SchemaGuard(kb.workspace).check if layout == "missing_column" else SchemaGuard.check_indexes
        with pytest.raises(ConfigurationError, match="offline workspace upgrade required"):
            check(connection)

    await kb.workers.control(alter)
    with pytest.raises(ConfigurationError):
        async with KnowledgeBase.open(kb.config):
            pass


@pytest.mark.parametrize("purge_pending", [0, 1])
async def test_corrupt_actual_peer_keeps_last_trusted_locator(kb, orphan_case, purge_pending):
    runner, job_id, digest, path = orphan_case
    peer_id = str(uuid4())

    def peer(connection, _token):
        JobStore.insert(connection, peer_id, "ingest", "bulk", {})
        claim = JobStore.claim(connection, "bulk", "ingest")
        assert claim is not None
        JobStore.checkpoint(connection, claim, {"verified_sha256": digest, "bytes": 12})
        JobStore.finish(connection, claim, "failed", {})
        connection.execute(
            "UPDATE jobs SET progress='invalid',finished_at=1,purge_pending=? WHERE uuid=?", (purge_pending, peer_id)
        )

    await kb.workers.control(peer)
    await runner._purge_step(job_id)
    assert path.exists()
    assert (
        await kb.workers.read(lambda c, _t: c.execute("SELECT blob_sha256 FROM jobs WHERE uuid=?", (peer_id,)).get)
        == digest
    )
    assert await kb.workers.read(lambda c, _t: c.execute("SELECT 1 FROM jobs WHERE uuid=?", (job_id,)).get) is None
    result = await kb.jobs({"action": "get", "job_id": peer_id})
    assert result["needs_attention"]
    if purge_pending:
        with pytest.raises(StorageIOError):
            await runner._purge_step(peer_id)
        assert path.exists()
