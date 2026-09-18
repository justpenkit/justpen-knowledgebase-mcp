"""Durable deletion jobs complete bounded steps across parents and lost metadata."""

import asyncio
import json
from uuid import uuid4

import pytest

from justpen_knowledgebase_mcp.catalog import validate_record
from justpen_knowledgebase_mcp.errors import ConflictError, RecordConflictError
from justpen_knowledgebase_mcp.identity import identity_key
from justpen_knowledgebase_mcp.models import DeleteRequest
from justpen_knowledgebase_mcp.storage.graph import _validate_endpoints, row_by_id
from justpen_knowledgebase_mcp.storage.job_recovery import recover_intents
from justpen_knowledgebase_mcp.storage.jobs import JobStore

pytestmark = pytest.mark.integration


async def test_two_parent_jobs_lost_metadata_cancel_and_bounded_completion(kb):
    await kb.job_runner.close()
    created = await kb.write(
        {
            "nodes": [
                {"type": "domain", "properties": {"value": f"{name}.example"}}
                for name in ["first", "second", "survivor"]
            ]
        }
    )
    first, second, survivor = [node["id"] for node in created["nodes"]]

    def populate(connection, _token):
        ids = dict(connection.execute("select uuid,id from nodes"))
        for owner in [first, second]:
            connection.executemany(
                "insert into relations(uuid,source_id,target_id,type,key,properties) values(?,?,?,'has_srv_target',?,?)",
                (
                    (
                        str(uuid4()),
                        ids[owner],
                        ids[survivor],
                        identity_key(
                            "relations",
                            "has_srv_target",
                            {"service": "_fixture", "protocol": "_tcp", "port": index, "priority": 0, "weight": 0},
                        ),
                        json.dumps(
                            {"service": "_fixture", "protocol": "_tcp", "port": index, "priority": 0, "weight": 0}
                        ),
                    )
                    for index in range(350)
                ),
            )

    await kb.workers.write(populate)

    def validate_fixture(connection, _token):
        for source, target, kind, key, raw in connection.execute(
            "select source_id,target_id,type,key,properties from relations"
        ):
            properties = json.loads(raw)
            validate_record("relations", kind, properties)
            source_row = row_by_id(connection, "nodes", source)
            target_row = row_by_id(connection, "nodes", target)
            assert source_row is not None
            assert target_row is not None
            _validate_endpoints(kind, source_row, target_row)
            assert key == identity_key("relations", kind, properties)

    await kb.workers.read(validate_fixture)
    jobs = [str(uuid4()), str(uuid4())]
    for owner, job_id in zip([first, second], jobs, strict=True):
        await kb.workers.write(
            lambda c, t, owner=owner, job_id=job_id: JobStore.admit_delete(
                c, DeleteRequest(kind="nodes", ids=[owner], cascade=True), job_id
            )
        )
        with pytest.raises(ConflictError):
            await kb.workers.control(lambda c, t, job_id=job_id: JobStore.cancel(c, job_id))
    with pytest.raises(RecordConflictError) as repeated:
        await kb.delete({"kind": "nodes", "ids": [first], "cascade": True})
    assert str(repeated.value.details.delete_job_id) == jobs[0]
    await kb.write({"nodes": [{"id": survivor, "label": "ready endpoint remains editable"}]})
    await kb.workers.control(lambda c, t: c.execute("delete from jobs where uuid=?", (jobs[0],)))
    recovered = await kb.workers.control(lambda c, t: recover_intents(c, "nodes", 0))
    assert recovered["repaired"] == 1
    assert (
        await kb.workers.read(lambda c, t: c.execute("select delete_job_id from nodes where uuid=?", (first,)).get)
        == jobs[0]
    )
    progress = {job_id: [] for job_id in jobs}
    claim_order = []
    while True:
        claim = await kb.workers.control(lambda c, t: JobStore.claim(c, "short", "delete"))
        if claim is None:
            break
        claim_order.append(claim.job_id)
        before = claim.progress.get("rows_deleted", 0)
        await kb.workers.control(lambda c, t, claim=claim: JobStore.delete_step(c, claim))
        row = await kb.jobs({"action": "get", "job_id": claim.job_id})
        after = row.get("progress", {}).get("rows_deleted", before)
        progress[claim.job_id].append(after - before)
        assert 0 <= after - before <= 100
        if row["state"] == "queued":
            with pytest.raises(ConflictError):
                await kb.workers.control(lambda c, t, claim=claim: JobStore.cancel(c, claim.job_id))
        await asyncio.sleep(0)
    assert set(claim_order) == set(jobs)
    # Same-kind jobs are FIFO; bounded group yielding does not promise ABAB order.
    assert all(progress.values())
    for job_id in jobs:
        assert (await kb.jobs({"action": "get", "job_id": job_id}))["state"] == "completed"
    assert (await kb.get({"kind": "nodes", "ids": [first, second]}))["missing_ids"] == [first, second]
    assert (await kb.get({"kind": "nodes", "ids": [survivor]}))["records"][0][
        "label"
    ] == "ready endpoint remains editable"
    assert await kb.workers.read(lambda c, t: c.execute("select count(*) from relations").get) == 0
    assert await kb.workers.read(lambda c, t: c.execute("pragma foreign_key_check").fetchall()) == []
