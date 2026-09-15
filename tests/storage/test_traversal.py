"""Bounded breadth-first traversal over actual adjacency indexes."""

import time

import pytest

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import InvalidParamsError, LimitError
from justpen_knowledgebase_mcp.models import NeighborsRequest, SearchRequest, WriteRequest
from justpen_knowledgebase_mcp.mutations import canonical_json
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.traversal import neighbors
from justpen_knowledgebase_mcp.storage.worker import OperationToken

from .graph_fixtures import admit

pytestmark = pytest.mark.integration


async def test_depth_direction_and_node_budget(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        result = await kb.write(
            WriteRequest.model_validate(
                {
                    "nodes": [
                        {"type": "domain", "properties": {"name": name}}
                        for name in ["example.com", "a.example.com", "b.a.example.com", "c.b.a.example.com"]
                    ]
                }
            )
        )
        ids = [item["id"] for item in result["nodes"]]
        await kb.write(
            WriteRequest.model_validate(
                {
                    "relations": [
                        {
                            "type": "subdomain_of",
                            "source_ref": {"id": ids[i + 1]},
                            "target_ref": {"id": ids[i]},
                            "properties": {},
                        }
                        for i in range(3)
                    ]
                }
            )
        )
        zero = await kb.neighbors(NeighborsRequest(seed_ids=[ids[0]], depth=0))
        assert [node["id"] for node in zero["nodes"]] == ids[:1]
        incoming = await kb.neighbors(NeighborsRequest(seed_ids=[ids[0]], direction="in", depth=3))
        assert [node["id"] for node in incoming["nodes"]] == ids
        assert len(incoming["edges"]) == 3
        outgoing = await kb.neighbors(NeighborsRequest(seed_ids=[ids[0]], direction="out", depth=3))
        assert len(outgoing["nodes"]) == 1
        bounded = await kb.neighbors(NeighborsRequest(seed_ids=[ids[0]], depth=3, max_nodes=2))
        assert len(bounded["nodes"]) == 2
        assert bounded["truncated"]
        assert bounded["reason"] == "max_nodes"
        assert bounded["frontier"]


async def test_cycles_multiple_seeds_types_and_edge_budget(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        result = await kb.write(
            WriteRequest.model_validate(
                {"nodes": [{"type": "hostname", "properties": {"name": f"h{i}.example"}} for i in range(3)]}
            )
        )
        ids = [item["id"] for item in result["nodes"]]
        await kb.write(
            WriteRequest.model_validate(
                {
                    "relations": [
                        {
                            "type": "aliases",
                            "source_ref": {"id": ids[i]},
                            "target_ref": {"id": ids[(i + 1) % 3]},
                            "properties": {"vantage": "test"},
                        }
                        for i in range(3)
                    ]
                }
            )
        )
        result = await kb.neighbors(NeighborsRequest(seed_ids=ids[:2], depth=3))
        assert len(result["nodes"]) == len(result["edges"]) == 3
        assert not result["truncated"]
        limited = await kb.neighbors(NeighborsRequest(seed_ids=ids[:1], max_edges=1, depth=3))
        assert len(limited["edges"]) == 1
        assert limited["reason"] == "max_edges"
        empty = await kb.neighbors(NeighborsRequest(seed_ids=ids[:1], relation_types=["resolves_to"]))
        assert empty["edges"] == []
        selected = await kb.neighbors(NeighborsRequest(seed_ids=ids[:1], relation_types=["aliases"]))
        assert len(selected["edges"]) == 2
        with pytest.raises(InvalidParamsError):
            await kb.write(
                WriteRequest.model_validate(
                    {
                        "relations": [
                            {
                                "type": "aliases",
                                "source_ref": {"id": ids[0]},
                                "target_ref": {"id": ids[0]},
                                "properties": {"vantage": "test"},
                            }
                        ]
                    }
                )
            )


async def test_high_degree_reads_bounded_adjacency_and_deadlines(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        result = await kb.write(
            WriteRequest.model_validate(
                {"nodes": [{"type": "hostname", "properties": {"name": f"h{i}.example"}} for i in range(100)]}
            )
        )
        ids = [item["id"] for item in result["nodes"]]
        await kb.write(
            WriteRequest.model_validate(
                {
                    "relations": [
                        {
                            "type": "aliases",
                            "source_ref": {"id": ids[0]},
                            "target_ref": {"id": other},
                            "properties": {"vantage": "test"},
                        }
                        for other in ids[1:]
                    ]
                }
            )
        )

        def run(connection, token):
            queries = []

            def trace(cursor, sql, bindings):
                queries.append(sql)
                return True

            connection.set_exec_trace(trace)
            try:
                result = neighbors(
                    connection, token, NeighborsRequest(seed_ids=ids[:1], max_nodes=2, relation_types=["aliases"])
                )
            finally:
                connection.set_exec_trace(None)
            return result, queries

        bounded, queries = await kb.workers.read(run)
        adjacency = [query for query in queries if "FROM relations o WHERE" in query]
        assert len(adjacency) == 4
        assert all("LIMIT 1" in query for query in adjacency)
        assert bounded["reason"] == "max_nodes"

        def near_deadline(connection, token):
            token.deadline = time.monotonic() + 0.008
            return neighbors(connection, token, NeighborsRequest(seed_ids=ids[:1]))

        partial = await kb.workers.read(near_deadline)
        assert partial["reason"] == "deadline"
        assert partial["frontier"] == ids[:1]
        with pytest.raises(LimitError):
            await kb.workers.read(
                lambda connection, token: neighbors(connection, token, NeighborsRequest(seed_ids=ids[:1])),
                OperationToken(time.monotonic() - 1),
            )


async def test_response_budget_keeps_frontier_and_visited_bounded(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        ids = []
        for start in range(0, 400, 100):
            result = await kb.write(
                WriteRequest.model_validate(
                    {
                        "nodes": [
                            {"type": "hostname", "properties": {"name": f"h{i}.example"}}
                            for i in range(start, start + 100)
                        ]
                    }
                )
            )
            ids.extend(item["id"] for item in result["nodes"])
        relations = [
            {
                "type": "aliases",
                "source_ref": {"id": ids[0]},
                "target_ref": {"id": other},
                "properties": {"vantage": str(vantage)},
            }
            for other in ids[1:]
            for vantage in range(3)
        ]
        for start in range(0, len(relations), 100):
            await kb.write(WriteRequest.model_validate({"relations": relations[start : start + 100]}))
        result = await kb.neighbors(NeighborsRequest(seed_ids=ids[:1], depth=3, max_nodes=1000, max_edges=3000))
        assert result["truncated"]
        assert result["reason"] == "response_bytes"
        assert len(result["nodes"]) <= 1000
        assert len(result["edges"]) <= 3000
        assert len(result["frontier"]) <= 1000
        assert len(canonical_json(result).encode("utf-8")) <= 262144


async def test_pending_visibility_matches_graph_readiness(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        result = await kb.write(
            WriteRequest.model_validate(
                {"nodes": [{"type": "hostname", "properties": {"name": f"h{i}.example"}} for i in range(3)]}
            )
        )
        ids = [item["id"] for item in result["nodes"]]
        created = await kb.write(
            WriteRequest.model_validate(
                {
                    "relations": [
                        {
                            "type": "aliases",
                            "source_ref": {"id": ids[0]},
                            "target_ref": {"id": other},
                            "properties": {"vantage": "test"},
                        }
                        for other in ids[1:]
                    ]
                }
            )
        )
        edge_ids = [item["id"] for item in created["relations"]]
        await admit(kb, "relations", edge_ids[:1])
        assert len((await kb.neighbors(NeighborsRequest(seed_ids=ids[:1])))["edges"]) == 1
        assert len((await kb.search(SearchRequest(kind="nodes")))["results"]) == 3
        await admit(kb, "nodes", ids[2:])
        assert (await kb.neighbors(NeighborsRequest(seed_ids=ids[:1])))["edges"] == []
        assert (await kb.search(SearchRequest(kind="relations")))["results"] == []
