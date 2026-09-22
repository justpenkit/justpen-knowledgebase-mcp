"""Bounded terminal retention, retry fencing and managed-file cleanup."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from justpen_knowledgebase_mcp import jobs
from justpen_knowledgebase_mcp.config import ServerConfig, WorkspacePolicy
from justpen_knowledgebase_mcp.errors import BusyError, ConflictError, NotFoundError
from justpen_knowledgebase_mcp.identity import identity_key
from justpen_knowledgebase_mcp.models import GetRequest, WriteRequest
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.connection import SQLiteRuntime
from justpen_knowledgebase_mcp.storage.job_retention import CANDIDATES_SQL, JobRetention
from justpen_knowledgebase_mcp.storage.jobs import JobStore
from justpen_knowledgebase_mcp.workspace import WorkspacePaths

pytestmark = pytest.mark.integration


def terminal(connection, state="completed", finished: float = 1, payload=None, progress=None):
    identifier = str(uuid4())
    JobStore.insert(connection, identifier, "ingest", "bulk", payload or {})
    claim = JobStore.claim(connection, "bulk", "ingest", now=finished - 1)
    assert claim is not None
    if progress:
        connection.execute("update jobs set progress=? where uuid=?", (json.dumps(progress), identifier))
    JobStore.finish(connection, claim, state, {}, now=finished)
    return identifier


def persist_policy(connection, **overrides):
    values = json.loads(connection.execute("select policy from settings").get)
    values.update(overrides)
    policy = WorkspacePolicy.model_validate(values)
    connection.execute("update settings set policy=?", (policy.model_dump_json(),))
    return policy


async def test_age_or_count_prunes_oldest_in_hundred_row_batches(kb):
    await kb.job_runner.close()
    policy = await kb.workers.control(
        lambda c, _t: persist_policy(c, completed_retention_count=2, failed_cancelled_retention_count=2)
    )

    def populate(c, _t):
        ids = [terminal(c, finished=100 + index) for index in range(205)]
        for state in ("failed", "cancelled", "failed"):
            terminal(c, state, 300)
        terminal(c, "failed", 1)
        return ids

    ids = await kb.workers.control(populate)
    cursor = (0.0, 0)
    for _ in range(3):
        outcome = await kb.workers.control(lambda c, _t, cursor=cursor: JobRetention.batch(c, policy, cursor, now=400))
        assert outcome["examined"] <= 100
        assert outcome["pruned"] <= 100
        cursor = outcome["cursor"]
    remaining = await kb.workers.read(
        lambda c, _t: [
            row[0] for row in c.execute("select uuid from jobs where state='completed' order by finished_at,id")
        ]
    )
    assert remaining == ids[-2:]
    counts = await kb.workers.read(lambda c, _t: json.loads(c.execute("select terminal_job_counts from settings").get))
    assert counts == {"completed": 2, "failed_cancelled": 2}
    expired_policy = await kb.workers.control(
        lambda c, _t: persist_policy(c, completed_retention_seconds=10, failed_cancelled_retention_seconds=10)
    )
    await kb.workers.control(lambda c, _t: JobRetention.batch(c, expired_policy, (0.0, 0), now=400))
    assert await kb.workers.read(lambda c, _t: c.execute("select count(*) from jobs").get) == 0


async def test_pending_owner_and_live_lease_are_protected_and_keyset_progresses(kb):
    await kb.job_runner.close()

    def populate(c, _t):
        ids = [terminal(c, "failed") for _ in range(101)]
        for identifier in ids[:100]:
            properties = {"value": f"pending-{identifier}.example"}
            c.execute(
                "insert into nodes(uuid,type,key,properties,lifecycle,delete_job_id,delete_cascade,delete_requested_at) "
                "values(?,'domain',?,?,'delete_pending',?,1,1)",
                (
                    str(uuid4()),
                    identity_key("nodes", "domain", properties),
                    json.dumps(properties),
                    identifier,
                ),
            )
        c.execute("update jobs set lease_expires_at=1000 where uuid=?", (ids[-1],))
        last = terminal(c, "cancelled", 2)
        return ids, last

    ids, last = await kb.workers.control(populate)
    policy = await kb.workers.control(
        lambda c, _t: persist_policy(c, failed_cancelled_retention_seconds=1, failed_cancelled_retention_count=1)
    )
    first = await kb.workers.control(lambda c, _t: JobRetention.batch(c, policy, (0.0, 0), now=20))
    assert first["pruned"] == 0
    second = await kb.workers.control(lambda c, _t: JobRetention.batch(c, policy, first["cursor"], now=20))
    assert second["pruned"] == 1
    assert (await kb.jobs({"action": "get", "job_id": ids[0]}))["retention_protected"] is True
    with pytest.raises(NotFoundError):
        await kb.jobs({"action": "get", "job_id": last})


async def test_purge_pending_fences_retry_until_every_recorded_stage_is_acknowledged(kb):
    await kb.job_runner.close()
    input_token, stage_token = str(uuid4()), str(uuid4())

    def populate(c, _t):
        identifier = terminal(c, "failed", payload={"input_token": input_token}, progress={"stage_token": stage_token})
        c.execute(
            "update jobs set payload=json_set(payload,'$.input_stage',?) where uuid=?",
            (f"{identifier}.{input_token}.stage", identifier),
        )
        return identifier

    identifier = await kb.workers.control(populate)
    outcome = await kb.workers.control(lambda c, _t: JobRetention.batch(c, WorkspacePolicy(), (0.0, 0), now=40 * 86400))
    assert outcome["marked"] == 1
    with pytest.raises(ConflictError, match="JOB_PURGING"):
        await kb.jobs({"action": "retry", "job_id": identifier})
    assert await kb.workers.control(lambda c, _t: JobRetention.next_file(c, identifier)) == input_token
    await kb.workers.control(lambda c, _t: JobRetention.acknowledge(c, identifier, input_token))
    assert await kb.workers.control(lambda c, _t: JobRetention.next_file(c, identifier)) == stage_token
    await kb.workers.control(lambda c, _t: JobRetention.acknowledge(c, identifier, stage_token))
    assert await kb.workers.control(lambda c, _t: JobRetention.finalize(c, identifier)) is True
    assert await kb.workers.control(lambda c, _t: JobRetention.finalize(c, identifier)) is False
    with pytest.raises(NotFoundError):
        await kb.jobs({"action": "retry", "job_id": identifier})


async def test_real_short_purge_preserves_blob_links_and_removes_recorded_files(kb, monkeypatch):
    monkeypatch.setattr(JobRetention, "next_job", staticmethod(lambda _c, _after=0: None))
    result = await kb.ingest_evidence({"base64": "AP8=", "source": "retained-source"})
    identifier = result["evidence_id"]
    graph = await kb.write(
        WriteRequest.model_validate(
            {
                "nodes": [
                    {
                        "type": "domain",
                        "properties": {"value": "retained.example"},
                        "evidence_add": [identifier],
                    }
                ]
            }
        )
    )
    node_id = graph["nodes"][0]["id"]
    token1, token2 = str(uuid4()), str(uuid4())

    def populate(c, _t):
        job_id = terminal(c, "failed", payload={"input_token": token1}, progress={"stage_token": token2})
        c.execute(
            "update jobs set payload=json_set(payload,'$.input_stage',?) where uuid=?",
            (f"{job_id}.{token1}.stage", job_id),
        )
        c.execute(
            "update evidence set index_owner_job_id=(select id from jobs where uuid=?) where uuid=?",
            (job_id, identifier),
        )
        return job_id

    job_id = await kb.workers.control(populate)
    for token in (token1, token2):
        (kb.workspace.tmp / f"{job_id}.{token}.stage").write_bytes(b"owned")
    outcome = await kb.workers.control(
        lambda c, _t: JobRetention.batch(c, kb.job_runner.store.policy, (0.0, 0), now=time.time())
    )
    assert outcome["marked"] == 1
    removed = []
    original = kb.job_runner.store.discard_stage

    def discard(job, token):
        if job == job_id:
            removed.append(token)
        original(job, token)

    monkeypatch.setattr(kb.job_runner.store, "discard_stage", discard)
    await kb.job_runner._purge_step(job_id)
    assert len(removed) == 1
    assert (await kb.jobs({"action": "get", "job_id": job_id}))["purge_pending"] is True
    await kb.job_runner._purge_step(job_id)
    assert removed == [token1, token2]
    assert (await kb.get(GetRequest(kind="nodes", ids=[node_id], view="links")))["links"] == [identifier]
    assert (await kb.get(GetRequest(kind="evidence", ids=[identifier], view="sources")))["sources"][0][
        "source"
    ] == "retained-source"
    with pytest.raises(NotFoundError):
        await kb.jobs({"action": "get", "job_id": job_id})
    assert (await kb.read_evidence({"evidence_id": identifier, "format": "base64"}))["content"] == "AP8="
    assert (
        await kb.workers.read(
            lambda c, _t: c.execute("select index_owner_job_id from evidence where uuid=?", (identifier,)).get
        )
        is None
    )


async def test_retention_status_cache_has_expiry_policy_and_explicit_unknown(kb):
    snapshot = kb.job_runner.retention_status()
    assert snapshot["policy"]["completed_retention_count"] == 100000
    assert snapshot["policy"]["failed_cancelled_retention_seconds"] == 30 * 86400
    await kb.job_runner.retention_pass(force=True)
    snapshot = kb.job_runner.retention_status()
    assert snapshot["available"] is True
    assert snapshot["stale"] is False
    assert snapshot["terminal_counts"] == {"completed": 0, "failed_cancelled": 0}
    await kb.workers.close()
    with pytest.raises(BusyError):
        await kb.job_runner.retention_pass(force=True)
    assert kb.job_runner.retention_status()["stale"] is True


async def test_old_unrecorded_stage_is_recovered_after_prune_without_touching_live_input(kb, monkeypatch):
    monkeypatch.setattr(JobRetention, "next_job", staticmethod(lambda _c, _after=0: None))
    old_token, live_token = str(uuid4()), str(uuid4())

    def populate(c, _t):
        old = terminal(c)
        live = terminal(c, "failed", time.time(), {"input_token": live_token})
        c.execute(
            "update jobs set payload=json_set(payload,'$.input_stage',?) where uuid=?",
            (f"{live}.{live_token}.stage", live),
        )
        return old, live

    old, live = await kb.workers.control(populate)
    old_name, live_name = f"{old}.{old_token}.stage", f"{live}.{live_token}.stage"
    (kb.workspace.tmp / old_name).write_bytes(b"old unrecorded")
    (kb.workspace.tmp / live_name).write_bytes(b"retry input")
    (kb.workspace.tmp / "unrelated").write_bytes(b"leave")
    await kb.workers.control(lambda c, _t: JobRetention.batch(c, kb.job_runner.store.policy, (0.0, 0)))
    with pytest.raises(NotFoundError):
        await kb.jobs({"action": "get", "job_id": old})
    assert (kb.workspace.tmp / old_name).exists()
    await kb.job_runner._orphan_step(old_name)
    await kb.job_runner._orphan_step(live_name)
    assert not (kb.workspace.tmp / old_name).exists()
    assert (kb.workspace.tmp / live_name).read_bytes() == b"retry input"
    assert (kb.workspace.tmp / "unrelated").read_bytes() == b"leave"


async def test_counter_reconciliation_is_explicit_and_prune_plan_uses_sparse_indexes(kb):
    await kb.job_runner.close()

    def populate(c, _t):
        terminal(c)
        terminal(c, "failed")
        terminal(c, "cancelled")
        c.execute('update settings set terminal_job_counts=\'{"completed":0,"failed_cancelled":0}\'')
        JobRetention.reconcile(c)
        plans = [row[3] for row in c.execute("explain query plan " + CANDIDATES_SQL, (0, 0) * 3)]
        purge = [
            row[3]
            for row in c.execute(
                "explain query plan select state,count(*) from jobs where purge_pending=1 group by state"
            )
        ]
        return JobRetention.counts(c), plans, purge

    counts, plans, purge = await kb.workers.control(populate)
    assert counts == {"completed": 1, "failed_cancelled": 2}
    assert sum("jobs_terminal" in item for item in plans) >= 3
    assert any("jobs_purge" in item for item in purge)


async def test_expiry_metadata_and_cached_protected_count(kb):
    await kb.job_runner.close()

    def populate(c, _t):
        job_id = terminal(c, "failed", 100)
        c.execute(
            "insert into evidence(uuid,sha256,byte_size,media_type,encoding,blob_path,lifecycle,delete_job_id,delete_cascade,delete_requested_at) values(?,?,0,'image/png','auto',?,'delete_pending',?,1,1)",
            ("e_" + "f" * 64, "f" * 64, "ff/ff/" + "f" * 64, job_id),
        )
        return job_id

    job_id = await kb.workers.control(populate)
    value = await kb.jobs({"action": "get", "job_id": job_id})
    assert value["expires_at"] == "1970-01-31T00:01:40.000000Z"
    assert value["retention_protected"] is True
    assert value["needs_attention"] is True
    await kb.job_runner.retention_pass(force=True)
    status = kb.job_runner.retention_status()
    assert status["protected_count"] == 1
    assert status["needs_attention"] is True
    assert status["pending_prune_count"] == 0
    assert status["terminal_counts"]["failed_cancelled"] == 1


async def test_automatic_count_cleanup_progresses_while_bulk_copy_is_blocked(tmp_path, monkeypatch):
    config = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(config) as workspace, SQLiteRuntime(workspace, config) as factory:
        connection = factory.connect()
        policy = json.loads(connection.execute("select policy from settings").get)
        policy["completed_retention_count"] = 2
        connection.execute("update settings set policy=?", (json.dumps(policy),))
        connection.close()
    (tmp_path / "bulk.bin").write_bytes(b"b" * 300000)
    async with KnowledgeBase.open(config) as kb:
        entered, release = threading.Event(), threading.Event()
        copy = kb.job_runner.store.copy_path

        def blocked(*args, **kwargs):
            entered.set()
            release.wait(5)
            return copy(*args, **kwargs)

        monkeypatch.setattr(kb.job_runner.store, "copy_path", blocked)
        bulk = await kb.ingest_evidence({"path": "bulk.bin"})
        assert await asyncio.to_thread(entered.wait, 2)
        deleted = asyncio.Event()
        loop = asyncio.get_running_loop()
        snapshot = JobRetention.snapshot
        first = await kb.ingest_evidence({"base64": "AA=="})

        def observe(c):
            result = snapshot(c)
            if c.execute("select 1 from jobs where uuid=?", (first["job_id"],)).get is None:
                loop.call_soon_threadsafe(deleted.set)
            return result

        monkeypatch.setattr(JobRetention, "snapshot", staticmethod(observe))
        # Let the completed startup sweep's count cooldown expire before the wake.
        kb.job_runner._retention_count_due = time.monotonic() - 1
        try:
            await kb.ingest_evidence({"base64": "AQ=="})
            await kb.ingest_evidence({"base64": "Ag=="})
            await asyncio.wait_for(deleted.wait(), 2)
            with pytest.raises(NotFoundError):
                await kb.jobs({"action": "get", "job_id": first["job_id"]})
            assert (await kb.read_evidence({"evidence_id": first["evidence_id"], "format": "base64"}))[
                "content"
            ] == "AA=="
            assert (await kb.jobs({"action": "get", "job_id": bulk["job_id"]}))["state"] == "running"
        finally:
            release.set()
        assert (await kb.job_runner.wait(bulk["job_id"], time.monotonic() + 3))["state"] == "completed"


async def test_unobserved_cache_is_explicit_and_status_never_queries_sql(kb, monkeypatch):
    unused = type(kb.job_runner)(kb.workers, kb.workspace, kb.job_runner.store.policy)
    try:
        value = unused.retention_status()
        assert value["available"] is False
        assert value["stale"] is True
        assert value["terminal_counts"] is None
        assert value["protected_count"] is None

        def forbidden(*_args):
            raise AssertionError("status must not query SQL")

        monkeypatch.setattr(kb.workers, "read", forbidden)
        assert kb.job_runner.retention_status()["available"] is True
    finally:
        await unused.close()


@pytest.mark.parametrize("progress", ["invalid", "[]", '{"bytes":NaN}', '{"verified_sha256":null}'])
async def test_corrupt_trusted_owner_keeps_its_locator_and_reports_attention(kb, progress):
    await kb.job_runner.close()
    digest = "a" * 64

    def populate(connection, _token):
        identifier = terminal(connection, "failed")
        connection.execute("UPDATE jobs SET blob_sha256=?,progress=? WHERE uuid=?", (digest, progress, identifier))
        return identifier

    identifier = await kb.workers.control(populate)
    await kb.job_runner.retention_pass(force=True)
    assert kb.job_runner.retention_status()["needs_attention"]
    result = await kb.jobs({"action": "get", "job_id": identifier})
    assert result["needs_attention"]
    assert result["progress"] == {"bytes": 0, "chunks": 0, "rows_deleted": 0}
    assert (
        await kb.workers.read(lambda c, _t: c.execute("SELECT blob_sha256 FROM jobs WHERE uuid=?", (identifier,)).get)
        == digest
    )


@pytest.mark.parametrize("progress", [{"bytes": -1}, {"chunks": []}, {"rows_deleted": "private"}])
async def test_invalid_counters_leave_durable_progress_and_blob_ownership_unchanged(kb, progress):
    await kb.job_runner.close()
    stored, digest = json.dumps(progress), "a" * 64

    def populate(connection, _token):
        identifier = terminal(connection, "failed")
        connection.execute("UPDATE jobs SET blob_sha256=?,progress=? WHERE uuid=?", (digest, stored, identifier))
        return identifier

    identifier = await kb.workers.control(populate)
    result = await kb.jobs({"action": "get", "job_id": identifier})
    assert result["needs_attention"]
    assert result["progress"] == {"bytes": 0, "chunks": 0, "rows_deleted": 0}
    assert "private" not in json.dumps(result)
    assert await kb.workers.read(
        lambda c, _t: c.execute("SELECT progress,blob_sha256 FROM jobs WHERE uuid=?", (identifier,)).fetchone()
    ) == (stored, digest)


async def test_healthy_history_does_not_scale_retention_snapshot_vm_work(kb):
    await kb.job_runner.close()
    measurements = []
    for size in (64, 4096):

        def populate(connection, _token, size=size):
            previous = connection.execute("SELECT count(*) FROM jobs").get
            for _ in range(previous, size):
                JobStore.insert(connection, str(uuid4()), "ingest", "short", {})
            connection.execute("UPDATE jobs SET state='completed',finished_at=100 WHERE state='queued'")
            JobRetention.reconcile(connection)

        await kb.workers.control(populate)

        def measure(connection, _token):
            instructions = 0

            def progress():
                nonlocal instructions
                instructions += 1
                return False

            connection.set_progress_handler(progress, 1, id="retention-snapshot-test")
            try:
                snapshot = JobRetention.snapshot(connection)
            finally:
                connection.set_progress_handler(None, id="retention-snapshot-test")
            return instructions, snapshot

        instructions, snapshot = await kb.workers.read(measure)
        assert not snapshot["needs_attention"]
        assert snapshot["terminal_counts"]["completed"] == size
        measurements.append(instructions)
    assert measurements[1] <= measurements[0] + 100, measurements


@pytest.mark.parametrize("payload", ["invalid", "[]", '{"all":"false"}'])
async def test_corrupt_cancel_preserves_row_counters_and_owned_file(kb, payload):
    await kb.job_runner.close()
    identifier, token = str(uuid4()), str(uuid4())
    stage = kb.workspace.tmp / f"{identifier}.{token}.stage"
    stage.write_bytes(b"retained")

    def populate(connection, _token):
        # The reindex expression index itself rejects syntactically invalid JSON.
        kind = "ingest" if payload == "invalid" else "reindex"
        JobStore.insert(connection, identifier, kind, "bulk", {})
        connection.execute("UPDATE jobs SET payload=?,blob_sha256=? WHERE uuid=?", (payload, "a" * 64, identifier))

    def snapshot(connection, _token):
        return (
            connection.execute("SELECT * FROM jobs WHERE uuid=?", (identifier,)).fetchone(),
            JobRetention.counts(connection),
        )

    await kb.workers.control(populate)
    before = await kb.workers.read(snapshot)
    with pytest.raises(ConflictError, match="JOB_METADATA_INVALID"):
        await kb.jobs({"action": "cancel", "job_id": identifier})
    assert await kb.workers.read(snapshot) == before
    assert stage.read_bytes() == b"retained"
    assert (await kb.jobs({"action": "get", "job_id": identifier}))["needs_attention"]


async def test_corrupt_retention_full_sweep_has_bounded_retries_and_preserves_owners(kb, monkeypatch):
    await kb.job_runner.close()
    policy = await kb.workers.control(lambda c, _t: persist_policy(c, failed_cancelled_retention_count=1))
    kb.job_runner.store.policy = policy

    def populate(connection, _token):
        identifiers = [terminal(connection, "failed") for _ in range(101)]
        connection.execute("UPDATE jobs SET progress='invalid',blob_sha256=?", ("a" * 64,))
        return identifiers

    identifiers = await kb.workers.control(populate)
    stage = kb.workspace.tmp / f"{identifiers[0]}.{uuid4()}.stage"
    stage.write_bytes(b"retained")
    clock = SimpleNamespace(value=100.0)
    monkeypatch.setattr(jobs, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock.value))
    batches = []
    original = JobRetention.batch

    def batch(connection, selected, cursor):
        result = original(connection, selected, cursor)
        batches.append(result)
        return result

    monkeypatch.setattr(JobRetention, "batch", batch)
    runner = kb.job_runner
    runner.last_error = "IO_ERROR: unexpected job failure"
    assert await runner.retention_pass(force=True)
    assert not await runner.retention_pass()
    assert [item["examined"] for item in batches] == [100, 1]
    for moment in (100.1, 115.0, 129.9):
        clock.value = moment
        runner._retention_event.set()
        assert not await runner.retention_pass()
    assert len(batches) == 2
    clock.value = 130.0
    assert await runner.retention_pass()
    assert not await runner.retention_pass()
    assert [item["examined"] for item in batches] == [100, 1, 100, 1]
    assert runner.retention_status()["needs_attention"]
    assert runner.retention_status()["last_error"] == "IO_ERROR: retention ownership metadata needs attention"
    assert runner.last_error == "IO_ERROR: unexpected job failure"
    assert await kb.workers.read(lambda c, _t: JobRetention.counts(c)) == {"completed": 0, "failed_cancelled": 101}
    assert await kb.workers.read(
        lambda c, _t: c.execute("SELECT DISTINCT progress,blob_sha256,purge_pending FROM jobs").fetchall()
    ) == [("invalid", "a" * 64, 0)]
    assert stage.read_bytes() == b"retained"


async def test_retention_corruption_stays_visible_after_count_falls_below_target(kb):
    await kb.job_runner.close()
    policy = await kb.workers.control(lambda c, _t: persist_policy(c, failed_cancelled_retention_count=1))
    kb.job_runner.store.policy = policy

    def populate(connection, _token):
        identifier = terminal(connection, "failed", time.time() - 2)
        terminal(connection, "failed", time.time() - 1)
        connection.execute("UPDATE jobs SET progress=? WHERE uuid=?", ('{"bytes":-1}', identifier))
        return identifier

    identifier = await kb.workers.control(populate)
    await kb.job_runner.retention_pass(force=True)
    await kb.job_runner.retention_pass(force=True)
    status = kb.job_runner.retention_status()
    assert status["terminal_counts"] == {"completed": 0, "failed_cancelled": 1}
    assert status["needs_attention"]
    assert status["last_error"] == "IO_ERROR: retention ownership metadata needs attention"
    assert await kb.workers.read(
        lambda c, _t: c.execute("SELECT progress,error_code,state FROM jobs WHERE uuid=?", (identifier,)).fetchone()
    ) == ('{"bytes":-1}', "JOB_METADATA_INVALID", "failed")

    # Simulate reviewed offline repair without introducing a production repair path.
    await kb.workers.control(
        lambda c, _t: c.execute("UPDATE jobs SET progress='{}',error_code=NULL WHERE uuid=?", (identifier,))
    )
    await kb.job_runner.retention_pass(force=True)
    assert not kb.job_runner.retention_status()["needs_attention"]
    assert kb.job_runner.retention_status()["last_error"] is None


async def test_batch_probes_each_owner_table_once_per_page_not_once_per_job(kb):
    """One retention batch must not scale its probe count with the page size."""
    await kb.job_runner.close()

    def populate(c, _t):
        ids = [terminal(c, "failed", finished=100 + index) for index in range(100)]
        # One protected owner inside the page: its probe result must survive batching.
        properties = {"value": f"pending-{ids[0]}.example"}
        c.execute(
            "insert into nodes(uuid,type,key,properties,lifecycle,delete_job_id,delete_cascade,delete_requested_at) "
            "values(?,'domain',?,?,'delete_pending',?,1,1)",
            (str(uuid4()), identity_key("nodes", "domain", properties), json.dumps(properties), ids[0]),
        )
        # One live lease inside the page: eligibility must still reject it.
        c.execute("update jobs set lease_expires_at=1000 where uuid=?", (ids[1],))
        return ids

    ids = await kb.workers.control(populate)
    policy = await kb.workers.control(
        lambda c, _t: persist_policy(c, failed_cancelled_retention_seconds=1, failed_cancelled_retention_count=1)
    )

    def measure(c, _t):
        observed: list[str] = []
        c.set_exec_trace(lambda _cursor, sql, _bindings: observed.append(sql) or True)
        try:
            outcome = JobRetention.batch(c, policy, (0.0, 0), now=400)
        finally:
            c.set_exec_trace(None)
        return outcome, observed

    outcome, observed = await kb.workers.control(measure)
    reads = [sql for sql in observed if sql.lstrip().upper().startswith(("SELECT", "WITH"))]
    assert outcome["examined"] == 100
    assert outcome["pruned"] == 98, outcome
    # The protected owner and the live lease are both retained.
    assert (await kb.jobs({"action": "get", "job_id": ids[0]}))["retention_protected"] is True
    assert (await kb.jobs({"action": "get", "job_id": ids[1]}))["state"] == "failed"
    assert len(reads) <= 12, f"{len(reads)} read statements for one 100-row page: {json.dumps(reads[:20])}"
