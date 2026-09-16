"""Bounded native query-work regressions from the Fable review."""

import hashlib
import importlib
import json
import os
import stat
from unittest.mock import Mock

import apsw
import pytest

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import LimitError
from justpen_knowledgebase_mcp.models import NeighborsRequest, SearchRequest, WriteRequest
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.evidence_records import EvidenceRecords
from justpen_knowledgebase_mcp.storage.fulltext import DOCUMENT_MATCH
from justpen_knowledgebase_mcp.storage.status import sample_derived_storage, sample_status
from justpen_knowledgebase_mcp.storage.traversal import neighbors

search_module = importlib.import_module("justpen_knowledgebase_mcp.storage.search")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("query", "mode", "sort"), [("common uniqueLAST", "words", "id"), ("common", "literal", "relevance")]
)
async def test_whole_search_vm_work_scales_with_candidates(tmp_path, query, mode, sort):
    measurements = []
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        for size in (64, 128):

            def populate(connection, _token, size=size):
                start = connection.execute("SELECT count(*) FROM evidence").get
                for index in range(start, size):
                    digest = f"{index:064x}"
                    connection.execute(
                        "INSERT INTO evidence(uuid,sha256,byte_size,blob_path,index_state,incomplete) VALUES(?,?,0,'fixture','ready',0)",
                        ("e_" + digest, digest),
                    )
                    identifier = connection.last_insert_rowid()
                    for chunk in ("common", f"unique{index}"):
                        connection.execute(
                            "INSERT INTO search_documents(evidence_id,text,index_generation,encoding) VALUES(?,?,0,'utf-8')",
                            (identifier, chunk),
                        )

            await kb.workers.write(populate)

            def probe(connection, token, size=size):
                profiles = []
                vm = 0

                def progress():
                    nonlocal vm
                    vm += 1
                    return False

                connection.set_progress_handler(progress, 1, id="query-review")
                connection.trace_v2(apsw.SQLITE_TRACE_PROFILE, profiles.append, id="query-review")
                try:
                    result = search_module.search(
                        connection,
                        token,
                        SearchRequest(
                            kind="evidence", query=query.replace("LAST", str(size - 1)), query_mode=mode, sort=sort
                        ),
                    )
                finally:
                    connection.trace_v2(0, None, id="query-review")
                    connection.set_progress_handler(None, id="query-review")
                assert len(result["items"]) == (1 if mode == "words" else 20)
                candidate_count = sum(item["sql"].startswith("SELECT o.id,o.uuid") for item in profiles)
                plan = list(connection.execute("EXPLAIN QUERY PLAN " + DOCUMENT_MATCH["evidence"], ('"common"', 1)))
                return {"size": size, "vm": vm, "candidate_statements": candidate_count, "plan": plan}

            measurements.append(await kb.workers.read(probe))
    (tmp_path / "query-work.json").write_text(json.dumps(measurements))
    assert measurements[1]["vm"] < 3 * measurements[0]["vm"], measurements
    assert all(item["candidate_statements"] == 1 for item in measurements), measurements


async def test_traversal_streams_each_selective_adjacency_once(tmp_path):

    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        written = await kb.write(
            WriteRequest.model_validate(
                {
                    "nodes": [
                        {"type": "domain", "properties": {"name": ("example" if i == 0 else f"h{i}.example")}}
                        for i in range(11)
                    ]
                }
            )
        )
        ids = [item["id"] for item in written["nodes"]]
        await kb.write(
            WriteRequest.model_validate(
                {
                    "relations": [
                        {
                            "type": "subdomain_of",
                            "source_ref": {"id": identifier},
                            "target_ref": {"id": ids[0]},
                            "properties": {},
                        }
                        for identifier in ids[1:]
                    ]
                }
            )
        )

        def probe(connection, token):
            plans = []
            statements = []

            def trace(_cursor, statement, bindings):
                if statement.startswith("SELECT o.id,o.uuid,o.type,o.source_id"):
                    statements.append((statement, bindings))
                return True

            connection.set_exec_trace(trace)
            try:
                request = NeighborsRequest(
                    seed_ids=[ids[0]], relation_types=["subdomain_of", "aliases", "resolves_to"] * 2
                )
                result = neighbors(connection, token, request)
            finally:
                connection.set_exec_trace(None)
            for statement, bindings in statements:
                plans.extend(connection.execute("EXPLAIN QUERY PLAN " + statement, bindings))
            assert len(result["edges"]) == 10
            assert [item["id"] for item in result["nodes"]] == ids
            assert len(statements) == 6
            assert all("TEMP B-TREE" not in str(row) for row in plans)
            assert sum("_type_id" in str(row) for row in plans) == 6

        await kb.workers.read(probe)


async def test_status_fallback_uses_sparse_indexes(tmp_path):

    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:

        def prepare(connection, _token):
            for index in range(50):
                metadata = json.dumps({"property_index": {"complete": index >= 2}})
                connection.execute(
                    "INSERT INTO nodes(uuid,type,key,properties,metadata) VALUES(?,'domain',?,'{}',?)",
                    (str(index), str(index), metadata),
                )
                if index:
                    connection.execute(
                        "INSERT INTO relations(uuid,type,key,source_id,target_id,properties,metadata) VALUES(?,'subdomain_of',?,1,?,'{}',?)",
                        (str(index), str(index), index + 1, metadata),
                    )

        await kb.workers.write(prepare)

        def probe(connection, token):
            statements = []
            connection.set_exec_trace(
                lambda _cursor, statement, bindings: statements.append((statement, bindings)) or True
            )
            try:
                result = sample_status(connection, token)
            finally:
                connection.set_exec_trace(None)
            plans = [
                list(connection.execute("EXPLAIN QUERY PLAN " + statement, bindings))
                for statement, bindings in statements
                if "json_extract" in statement
            ]
            return result, plans

        result, plans = await kb.workers.read(probe)
        assert result["property_index_fallback"] == {"nodes": 2, "relations": 1}
        assert "nodes_property_fallback" in str(plans)
        assert "relations_property_fallback" in str(plans)
        assert "SEARCH s USING INTEGER PRIMARY KEY" in str(plans)


async def test_status_derived_page_bytes_are_separate_from_canonical(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:

        def expected(connection, _token):
            rows = list(connection.execute("SELECT name,pgsize FROM dbstat WHERE aggregate=1"))
            projection = sum(
                size for name, size in rows if name == "search_documents" or name.startswith("search_documents_")
            )
            properties = sum(
                size
                for name, size in rows
                if "property_index" in name or "property_lookup" in name or name.endswith("_property_fallback")
            )
            return projection, properties

        size, properties = await kb.workers.read(expected)
        await kb.status_sampler.refresh_derived()
        status = await kb.status()
        storage = status["database"]["sample"]["derived_storage"]
        assert storage["available"] is True
        assert storage["text_projection_bytes"] == size > 0
        assert storage["fts_index_bytes"] > 0
        assert storage["property_index_bytes"] == properties > 0
        assert storage["measurement"] == "sqlite_page_allocation"


@pytest.mark.parametrize("failed_directory", [None, "hash_leaf", "hash_parent", "evidence", "tmp"])
async def test_publish_directory_durability_precedes_database_reference(tmp_path, monkeypatch, failed_directory):

    digest = hashlib.sha256(b"durability").hexdigest()
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        events = []
        paths = {
            "hash_leaf": kb.workspace.evidence / digest[:2] / digest[2:4],
            "hash_parent": kb.workspace.evidence / digest[:2],
            "evidence": kb.workspace.evidence,
            "tmp": kb.workspace.tmp,
        }
        synced = os.fsync
        renamed = os.rename
        published = EvidenceRecords.publish_record
        failed = False

        def sync(fd):
            nonlocal failed
            inode = os.fstat(fd)
            kind = (
                "file"
                if stat.S_ISREG(inode.st_mode)
                else next(
                    (name for name, path in paths.items() if path.exists() and path.stat().st_ino == inode.st_ino),
                    "other",
                )
            )
            if "rename" in events and kind == failed_directory and not failed:
                failed = True
                raise OSError("controlled directory sync failure")
            synced(fd)
            events.append(kind)

        def rename(*args, **kwargs):
            renamed(*args, **kwargs)
            events.append("rename")

        def publish(*args):
            assert events[events.index("rename") + 1 :] == ["hash_leaf", "hash_parent", "evidence", "tmp"]
            events.append("database")
            return published(*args)

        monkeypatch.setattr(os, "fsync", sync)
        monkeypatch.setattr(os, "rename", rename)
        monkeypatch.setattr(EvidenceRecords, "publish_record", publish)
        result = await kb.ingest_evidence({"text": "durability"})
        count = await kb.workers.read(
            lambda connection, _token: connection.execute("SELECT count(*) FROM evidence").get
        )
        assert "file" in events[: events.index("rename")]
        if failed_directory is None:
            assert result["state"] == "completed"
            assert count == 1
            assert "database" in events
        else:
            assert result["state"] == "failed"
            assert count == 0
            assert "database" not in events


def test_dbstat_unsupported_native_runtime_is_explicitly_unavailable():
    with apsw.Connection(":memory:") as connection:
        connection.execute("CREATE TABLE search_documents(text)")
        connection.drop_modules(keep=[])
        result = sample_derived_storage(connection, Mock())
        assert result == {"available": False, "reason": "DBSTAT_UNAVAILABLE"}
    connection.close()


async def test_dbstat_stream_selects_one_object_and_checks_each_page(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:

        def prepare(connection, _token):
            connection.execute("INSERT INTO nodes(uuid,type,key,properties) VALUES('fixture','domain','fixture','{}')")
            connection.execute("INSERT INTO search_documents(node_id,text) VALUES(1,?)", ("bounded " * 20000,))

        await kb.workers.write(prepare)

        def probe(connection, _token):
            plans = list(
                connection.execute("EXPLAIN QUERY PLAN SELECT pgsize FROM dbstat WHERE name=?", ("search_documents",))
            )
            token = Mock()
            token.check.side_effect = [None, None, LimitError("page cancellation")]
            try:
                sample_derived_storage(connection, token)
            except LimitError:
                interrupted = True
            else:
                interrupted = False
            return interrupted, token.check.call_count, plans, connection.execute("SELECT 1").get

        interrupted, checks, plans, healthy = await kb.workers.read(probe)
        assert interrupted
        assert checks == 3
        assert healthy == 1
        assert "INDEX 0x2:" in str(plans)
        assert "TEMP B-TREE" not in str(plans)
