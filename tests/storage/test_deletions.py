"""Bounded SQL deletion primitives; job fixtures stand in for Task6 orchestration."""

from uuid import uuid4

import pytest

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import ConflictError, MissingRecordsError, NotFoundError, RecordConflictError
from justpen_knowledgebase_mcp.models import GetRequest, WriteRequest
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.deletions import GraphDeletion

from .graph_fixtures import admit, evidence_fixture

pytestmark = pytest.mark.integration


async def graph(kb):
    return await kb.write(
        WriteRequest.model_validate(
            {
                "nodes": [
                    {"type": "domain", "properties": {"name": "a.example"}},
                    {"type": "domain", "properties": {"name": "example"}},
                ],
                "relations": [
                    {
                        "type": "subdomain_of",
                        "source_ref": {"node_index": 0},
                        "target_ref": {"node_index": 1},
                        "properties": {},
                    }
                ],
            }
        )
    )


async def test_atomic_missing_pending_and_dependency_admission(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        result = await graph(kb)
        a, c = [row["id"] for row in result["nodes"]]
        relation = result["relations"][0]["id"]
        with pytest.raises(NotFoundError):
            await admit(kb, "nodes", [a, str(uuid4())])
        assert await kb.workers.read(lambda c, t: c.execute("select count(*) from jobs").get) == 0
        with pytest.raises(ConflictError, match="DEPENDENCIES_EXIST"):
            await admit(kb, "nodes", [a], cascade=False)
        intents = await admit(kb, "nodes", [a])
        with pytest.raises(ConflictError, match="RECORD_DELETING"):
            await admit(kb, "nodes", [c, a])
        with pytest.raises(RecordConflictError, match="DEPENDENCIES_EXIST") as caught:
            await admit(kb, "nodes", [c], cascade=False)
        assert caught.value.details.blocking_record.id == __import__("uuid").UUID(relation)
        assert str(caught.value.details.delete_job_id) == intents[0].job_id
        assert caught.value.details.deletion_owner is not None
        assert str(caught.value.details.deletion_owner.id) == a
        record = (await kb.get(GetRequest(kind="relations", ids=[relation])))["records"][0]
        assert record["lifecycle"] == "delete_pending"
        assert record["delete_job_id"] == intents[0].job_id
        await kb.write(WriteRequest.model_validate({"nodes": [{"id": c, "label": "still ready"}]}))
        with pytest.raises(ConflictError, match="RECORD_DELETING"):
            await kb.write(WriteRequest.model_validate({"nodes": [{"id": a}]}))
        while True:
            step = await kb.workers.write(
                lambda connection, token: GraphDeletion.step(connection, intents[0], row_budget=1)
            )
            assert step.rows_deleted <= 1
            if step.done:
                break
        assert (await kb.get(GetRequest(kind="nodes", ids=[c])))["records"][0]["label"] == "still ready"
        assert await kb.workers.read(lambda c, t: c.execute("select count(*) from relations").get) == 0


async def test_relation_without_evidence_does_not_require_cascade(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        result = await graph(kb)
        relation = result["relations"][0]["id"]
        intents = await admit(kb, "relations", [relation], cascade=False)
        await kb.write(WriteRequest.model_validate({"nodes": [{"id": result["nodes"][0]["id"], "label": "editable"}]}))
        step = await kb.workers.write(lambda c, t: GraphDeletion.step(c, intents[0]))
        assert step.done
        assert step.rows_deleted == 1
        assert await kb.workers.read(lambda c, t: c.execute("select count(*) from nodes").get) == 2


async def test_hub_admission_and_step_do_not_expand_one_hundred_thousand_edges(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        result = await graph(kb)
        owner = result["nodes"][0]["id"]

        def hub(connection, token):
            source, target = connection.execute("select source_id,target_id from relations").get
            connection.executemany(
                "insert into relations(uuid,source_id,type,target_id,key,properties) values (?,?, 'subdomain_of',?,?,'{}')",
                ((str(uuid4()), source, target, str(i)) for i in range(100000)),
            )

        await kb.workers.write(hub)
        intents = await admit(kb, "nodes", [owner])
        assert (
            await kb.workers.read(
                lambda c, t: c.execute("select count(*) from relations where lifecycle='delete_pending'").get
            )
            == 0
        )
        step = await kb.workers.write(lambda c, t: GraphDeletion.step(c, intents[0]))
        assert step.rows_deleted == 100
        assert not step.done
        assert await kb.workers.read(lambda c, t: c.execute("select count(*) from relations").get) == 99901


async def test_many_link_derived_rows_budget_and_evidence_file_handoff(tmp_path):

    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        result = await graph(kb)
        relation = result["relations"][0]["id"]
        evidence = await evidence_fixture(kb, 150)
        await kb.write(WriteRequest.model_validate({"relations": [{"id": relation, "evidence_add": evidence[:100]}]}))
        await kb.write(WriteRequest.model_validate({"relations": [{"id": relation, "evidence_add": evidence[100:]}]}))

        def derived(connection, token):
            identifier = connection.execute("select id from relations where uuid=?", (relation,)).get
            connection.executemany(
                "insert into search_documents(relation_id,text) values (?,?)", [(identifier, "body")] * 120
            )
            connection.executemany(
                "insert into relation_property_index(owner_id,path,value_type,value_materialized,value) values (?,?,'string',1,'text')",
                [(identifier, f"/p{i}") for i in range(120)],
            )

        await kb.workers.write(derived)
        with pytest.raises(ConflictError, match="DEPENDENCIES_EXIST"):
            await admit(kb, "relations", [relation], cascade=False)
        intents = await admit(kb, "relations", [relation])
        costs = []
        while True:
            step = await kb.workers.write(lambda c, t: GraphDeletion.step(c, intents[0]))
            costs.append(step.rows_deleted)
            if step.done:
                break
        assert costs == [100, 100, 100, 91]
        assert await kb.workers.read(lambda c, t: c.execute("select count(*) from evidence").get) == 150
        intent = (await admit(kb, "evidence", [evidence[0]]))[0]
        step = await kb.workers.write(lambda c, t: GraphDeletion.step(c, intent))
        assert step.files_pending
        assert not step.done
        assert (
            await kb.workers.read(
                lambda c, t: c.execute("select blob_path from evidence where uuid=?", (evidence[0],)).get
            )
            == "test"
        )


async def test_two_pending_parents_clean_shared_relation_idempotently(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        result = await graph(kb)
        first = (await admit(kb, "nodes", [result["nodes"][0]["id"]]))[0]
        second = (await admit(kb, "nodes", [result["nodes"][1]["id"]]))[0]
        first_step = await kb.workers.write(lambda c, t: GraphDeletion.step(c, first, 1))
        assert first_step.rows_deleted == 1
        second_step = await kb.workers.write(lambda c, t: GraphDeletion.step(c, second))
        assert second_step.done
        assert (await kb.workers.write(lambda c, t: GraphDeletion.step(c, first))).done
        assert (await kb.workers.write(lambda c, t: GraphDeletion.step(c, second))).rows_deleted == 0


@pytest.mark.parametrize("kind", ["nodes", "relations", "evidence"])
async def test_cascade_false_evidence_link_matrix_and_atomic_hundred_ids(tmp_path, kind):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        result = await graph(kb)
        evidence = (await evidence_fixture(kb, 1))[0]
        node, relation = result["nodes"][0]["id"], result["relations"][0]["id"]
        await kb.write(
            WriteRequest.model_validate(
                {
                    "nodes": [{"id": node, "evidence_add": [evidence]}],
                    "relations": [{"id": relation, "evidence_add": [evidence]}],
                }
            )
        )
        target = {"nodes": node, "relations": relation, "evidence": evidence}[kind]
        with pytest.raises(ConflictError, match="DEPENDENCIES_EXIST"):
            await admit(kb, kind, [target], cascade=False)
        with pytest.raises(MissingRecordsError) as caught:
            await admit(
                kb,
                kind,
                [target, *(("e_" + uuid4().hex * 2) if kind == "evidence" else str(uuid4()) for _ in range(99))],
            )
        assert len(caught.value.details.missing_ids) == 99
        assert await kb.workers.read(lambda c, t: c.execute("select count(*) from jobs").get) == 0
        assert (await kb.get(GetRequest(kind=kind, ids=[target])))["records"][0]["lifecycle"] == "ready"


async def test_hundred_target_pending_batch_has_no_partial_admission(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        result = await kb.write(
            WriteRequest.model_validate(
                {"nodes": [{"type": "hostname", "properties": {"name": f"h{i}"}} for i in range(100)]}
            )
        )
        identifiers = [row["id"] for row in result["nodes"]]
        await admit(kb, "nodes", [identifiers[-1]])
        with pytest.raises(RecordConflictError):
            await admit(kb, "nodes", identifiers)
        assert (
            await kb.workers.read(
                lambda c, t: c.execute("select count(*) from nodes where lifecycle='delete_pending'").get
            )
            == 1
        )
        assert await kb.workers.read(lambda c, t: c.execute("select count(*) from jobs").get) == 1
