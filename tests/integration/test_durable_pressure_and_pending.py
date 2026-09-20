"""Real accepted-job pressure interleavings and public pending-owner contracts."""

import asyncio
import hashlib
import json
import time

import pytest

from justpen_knowledgebase_mcp import jobs, reindex
from justpen_knowledgebase_mcp.errors import ConflictError, IndexingError, RecordConflictError, WalBusyError
from justpen_knowledgebase_mcp.storage.evidence_records import EvidenceRecords
from justpen_knowledgebase_mcp.storage.jobs import JobStore

pytestmark = pytest.mark.integration


def pressure(connection, phase):
    state = json.loads(connection.execute("select maintenance from settings").get)
    state.update(phase=phase, pressure_started_at=time.time() if phase == "pressure" else None)
    connection.execute("update settings set maintenance=?", (json.dumps(state),))


async def accept_without_wait(_job_id, _deadline, accepted=None):
    assert accepted is not None
    return accepted


async def admit_under_pressure(kb, monkeypatch, kind, text, evidence_id):
    fired = False
    if kind == "ingest":
        original = EvidenceRecords.check_existing

        def ingest_intercept(connection, claim, digest):
            nonlocal fired
            value = original(connection, claim, digest)
            if not fired:
                fired = True
                pressure(connection, "pressure")
            return value

        monkeypatch.setattr(EvidenceRecords, "check_existing", ingest_intercept)
        return await kb.ingest_evidence({"text": text})
    if kind == "reindex":
        original = reindex._publish_chunk

        def index_intercept(connection, claim, owner, chunk, count):
            nonlocal fired
            original(connection, claim, owner, chunk, count)
            if not fired:
                fired = True
                pressure(connection, "pressure")

        monkeypatch.setattr(reindex, "_publish_chunk", index_intercept)
        return await kb.reindex({"kind": "evidence", "ids": [evidence_id]})
    original = JobStore.delete_step

    def delete_intercept(connection, claim):
        nonlocal fired
        original(connection, claim)
        if not fired:
            fired = True
            pressure(connection, "pressure")

    monkeypatch.setattr(JobStore, "delete_step", delete_intercept)
    return await kb.delete({"kind": "evidence", "ids": [evidence_id], "cascade": True})


@pytest.mark.parametrize("kind", ["ingest", "delete", "reindex"])
async def test_accepted_job_survives_pressure_and_completes_without_retry(kb, monkeypatch, kind):
    text = "committed evidence before pressure\n"
    evidence_id = "e_" + hashlib.sha256(text.encode()).hexdigest()
    if kind != "ingest":
        assert (await kb.ingest_evidence({"text": text}))["state"] == "completed"
    # Hold maintenance steady so the product gate sees a deterministic persisted
    # pressure state; real worker admissions, transactions and jobs still run.
    await kb.maintenance.close()
    wait = kb.job_runner.wait
    monkeypatch.setattr(kb.job_runner, "wait", accept_without_wait)
    deferred = asyncio.Event()
    original_defer = kb.job_runner._defer_claim
    errors = []

    async def observe_defer(claim, error):
        errors.append(error)
        deferred.set()
        await original_defer(claim, error)

    monkeypatch.setattr(kb.job_runner, "_defer_claim", observe_defer)
    accepted = await admit_under_pressure(kb, monkeypatch, kind, text, evidence_id)
    identifier = accepted["job_id"]
    await asyncio.wait_for(deferred.wait(), 5)
    assert isinstance(errors[0], WalBusyError)

    def snapshot(connection, _token):
        return connection.execute(
            "select state,progress,payload,attempts from jobs where uuid=?", (identifier,)
        ).fetchone()

    before = await kb.workers.control(snapshot)
    assert before[0] == "running"
    owner = None
    progress = json.loads(before[1])
    if kind == "ingest":
        assert progress["verified_sha256"] == evidence_id[2:]
        assert progress["bytes"] == len(text)
        payload = json.loads(before[2])
        assert (kb.workspace.tmp / payload["input_stage"]).is_file()
    elif kind == "reindex":
        assert progress["chunks"] == 1
        owner = await kb.workers.control(
            lambda c, _t: c.execute(
                "select index_generation,index_owner_job_id,index_owner_token from evidence where uuid=?",
                (evidence_id,),
            ).fetchone()
        )
        assert owner[1] is not None
        assert owner[2] is not None
    else:
        intent = await kb.workers.control(
            lambda c, _t: c.execute(
                "select lifecycle,delete_job_id,delete_requested_at from evidence where uuid=?", (evidence_id,)
            ).fetchone()
        )
        assert intent[0:2] == ("delete_pending", identifier)
        assert intent[2] is not None
        with pytest.raises(ConflictError, match="DELETE_ALREADY_COMMITTED"):
            await kb.jobs({"action": "cancel", "job_id": identifier})
    await asyncio.sleep(0.15)
    assert await kb.workers.control(snapshot) == before
    assert len(errors) == 1  # no hot re-claim/fail loop during pressure
    with pytest.raises(WalBusyError):
        await kb.workers.write(lambda _c, _t: pytest.fail("product admission bypassed"))
    await kb.workers.control(lambda c, _t: pressure(c, "normal"))
    result = await wait(identifier, time.monotonic() + 10)
    assert result["job_id"] == identifier
    assert result["state"] == "completed"
    assert "error" not in result
    assert not result["needs_attention"]
    record = await kb.get({"kind": "evidence", "ids": [evidence_id]})
    if kind == "delete":
        assert record["missing_ids"] == [evidence_id]
    else:
        assert (await kb.read_evidence({"evidence_id": evidence_id}))["content"] == text
        assert record["records"][0]["index_state"] == "ready"
        assert not record["records"][0]["incomplete"]
        current = await kb.workers.read(
            lambda c, _t: c.execute(
                "select index_generation,index_owner_job_id,index_owner_token from evidence where uuid=?",
                (evidence_id,),
            ).fetchone()
        )
        assert current[1:] == (None, None)
        if kind == "reindex":
            assert owner is not None
            assert current[0] == owner[0]


@pytest.mark.parametrize("kind", ["ingest", "reindex"])
async def test_pending_evidence_race_persists_readable_public_job(kb, monkeypatch, kind):
    text = "raw evidence survives pending indexing race"
    evidence_id = "e_" + hashlib.sha256(text.encode()).hexdigest()
    if kind == "reindex":
        await kb.ingest_evidence({"text": text})
    entered, release, delete_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    wait = kb.job_runner.wait
    monkeypatch.setattr(kb.job_runner, "wait", accept_without_wait)
    original = jobs.index_evidence if kind == "ingest" else reindex.index_evidence

    async def pause_index(runner, claim, identifier):
        entered.set()
        await release.wait()
        return await original(runner, claim, identifier)

    original_delete = kb.job_runner._delete_step

    async def pause_delete(claim):
        await delete_release.wait()
        await original_delete(claim)

    monkeypatch.setattr(jobs if kind == "ingest" else reindex, "index_evidence", pause_index)
    monkeypatch.setattr(kb.job_runner, "_delete_step", pause_delete)
    try:
        accepted = (
            await kb.ingest_evidence({"text": text})
            if kind == "ingest"
            else await kb.reindex({"kind": "evidence", "ids": [evidence_id]})
        )
        await asyncio.wait_for(entered.wait(), 5)
        deletion = await kb.delete({"kind": "evidence", "ids": [evidence_id], "cascade": True})
        pending = (await kb.get({"kind": "evidence", "ids": [evidence_id]}))["records"][0]
        assert pending["lifecycle"] == "delete_pending"
        release.set()
        failed = await wait(accepted["job_id"], time.monotonic() + 5)
        assert failed["state"] == "failed"
        assert failed["error"] == "CONFLICT"
        assert failed["reason"] == "RECORD_DELETING"
        assert failed["details"] == {
            "blocking_record": {"kind": "evidence", "id": evidence_id},
            "delete_job_id": deletion["job_id"],
            "pending_since": pending["pending_since"],
        }
        public = await kb.jobs({"action": "get", "job_id": accepted["job_id"]})
        listed = await kb.jobs({"action": "list"})
        assert public["details"] == failed["details"]
        assert (
            next(item for item in listed["jobs"] if item["job_id"] == accepted["job_id"])["details"]
            == failed["details"]
        )
        blob = kb.workspace.evidence / kb.job_runner.store.blob_name(evidence_id[2:])
        assert blob.read_bytes() == text.encode()
        assert public["index_state"] == pending["index_state"]
        assert public["incomplete"] == pending["incomplete"]
    finally:
        release.set()
        delete_release.set()


@pytest.mark.parametrize("pending_kind", ["evidence", "nodes", "relations"])
async def test_association_pages_require_ready_owner_but_record_remains_inspectable(kb, monkeypatch, pending_kind):
    written = await kb.write(
        {
            "nodes": [
                {"type": "subdomain", "properties": {"value": "a.example.com"}},
                {"type": "domain", "properties": {"value": "example.com"}},
            ],
            "relations": [
                {
                    "type": "has_subdomain",
                    "source_ref": {"node_index": 1},
                    "target_ref": {"node_index": 0},
                    "properties": {},
                }
            ],
        }
    )
    node, relation = written["nodes"][0]["id"], written["relations"][0]["id"]
    evidence = (
        await kb.ingest_evidence(
            {
                "text": "linked proof",
                "source": "scanner",
                "targets": [{"kind": "nodes", "id": node}, {"kind": "relations", "id": relation}],
            }
        )
    )["evidence_id"]
    await kb.job_runner.close()
    monkeypatch.setattr(kb.job_runner, "wait", accept_without_wait)
    deleted_kind = "evidence" if pending_kind == "evidence" else "nodes"
    deleted_id = evidence if pending_kind == "evidence" else node
    owner_id = {"evidence": evidence, "nodes": node, "relations": relation}[pending_kind]
    deletion = await kb.delete({"kind": deleted_kind, "ids": [deleted_id], "cascade": True})
    record = (await kb.get({"kind": pending_kind, "ids": [owner_id]}))["records"][0]
    assert record["lifecycle"] == "delete_pending"
    assert record["delete_job_id"] == deletion["job_id"]
    for view in ["sources", "links"] if pending_kind == "evidence" else ["links"]:
        with pytest.raises(RecordConflictError) as failure:
            await kb.get({"kind": pending_kind, "ids": [owner_id], "view": view})
        assert failure.value.details.model_dump(mode="json", exclude_none=True) == {
            "blocking_record": {"kind": deleted_kind, "id": deleted_id},
            "delete_job_id": deletion["job_id"],
            "pending_since": record["pending_since"],
        }
    assert (await kb.get({"kind": "nodes", "ids": [written["nodes"][1]["id"]], "view": "links"}))["links"] == []


async def test_evidence_record_coverage_tracks_metadata_corrections(kb):
    imported = await kb.ingest_evidence({"base64": "/w=="})
    identifier = imported["evidence_id"]
    raw_only = (await kb.get({"kind": "evidence", "ids": [identifier]}))["records"][0]
    assert (raw_only["index_state"], raw_only["incomplete"]) == ("not_applicable", False)
    corrected = await kb.reindex(
        {"kind": "evidence", "ids": [identifier], "media_type": "text/plain", "encoding": "utf-8"}
    )
    failed = await kb.job_runner.wait(corrected["job_id"], time.monotonic() + 5)
    assert failed["state"] == "failed"
    record = (await kb.get({"kind": "evidence", "ids": [identifier]}))["records"][0]
    assert (record["index_state"], record["incomplete"]) == ("index_failed", True)
    assert not {"blob_path", "index_owner_job_id", "index_owner_token", "index_generation"} & record.keys()
    assert (await kb.read_evidence({"evidence_id": identifier, "format": "base64"}))["content"] == "/w=="


async def test_permanent_index_failure_under_pressure_remains_terminal(kb, monkeypatch):
    imported = await kb.ingest_evidence({"text": "decode failure fixture"})
    identifier = imported["evidence_id"]
    await kb.maintenance.close()

    async def fail(runner, _claim, _owner):
        await runner.workers.control(lambda c, _t: pressure(c, "pressure"))
        raise IndexingError("TEXT_DECODE_FAILED")

    monkeypatch.setattr(reindex, "_index_stream", fail)
    accepted = await kb.reindex({"kind": "evidence", "ids": [identifier]})
    deadline = time.monotonic() + 5
    while True:
        result = await kb.workers.control(lambda c, _t: JobStore.get(c, accepted["job_id"]))
        if result["state"] == "failed":
            break
        assert time.monotonic() < deadline
        await asyncio.sleep(0.01)
    assert result["error"] == "INDEX_ERROR"
    assert result["reason"] == "TEXT_DECODE_FAILED"
    assert result["index_state"] == "index_failed"
    owner = await kb.workers.control(
        lambda c, _t: c.execute(
            "select index_owner_job_id,index_owner_token from evidence where uuid=?", (identifier,)
        ).fetchone()
    )
    assert owner == (None, None)
    with pytest.raises(WalBusyError):
        await kb.workers.read(lambda _c, _t: pytest.fail("product gate bypassed"))


@pytest.mark.parametrize("kind", ["ingest", "reindex"])
async def test_cancel_during_pressure_backoff_finishes_same_job(kb, monkeypatch, kind):
    text = "cancel while pressure holds the job"
    evidence_id = "e_" + hashlib.sha256(text.encode()).hexdigest()
    if kind == "reindex":
        await kb.ingest_evidence({"text": text})
    await kb.maintenance.close()
    monkeypatch.setattr(kb.job_runner, "wait", accept_without_wait)
    entered = asyncio.Event()
    original = kb.job_runner._defer_claim

    async def defer(claim, error):
        entered.set()
        await original(claim, error)

    monkeypatch.setattr(kb.job_runner, "_defer_claim", defer)
    accepted = await admit_under_pressure(kb, monkeypatch, kind, text, evidence_id)
    await asyncio.wait_for(entered.wait(), 5)
    await kb.jobs({"action": "cancel", "job_id": accepted["job_id"]})
    deadline = time.monotonic() + 5
    while True:
        result = await kb.workers.control(lambda c, _t: JobStore.get(c, accepted["job_id"]))
        if result["state"] == "cancelled":
            break
        assert time.monotonic() < deadline
        await asyncio.sleep(0.01)
    assert result["job_id"] == accepted["job_id"]
    assert result["reason"] == "JOB_CANCELLED"
    with pytest.raises(WalBusyError):
        await kb.workers.read(lambda _c, _t: pytest.fail("pressure was cleared unexpectedly"))
