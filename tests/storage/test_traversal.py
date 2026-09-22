"""Bounded breadth-first traversal over actual adjacency indexes."""

import time

import apsw
import pytest

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import InvalidParamsError, LimitError
from justpen_knowledgebase_mcp.models import NeighborsRequest, SearchRequest, WriteRequest
from justpen_knowledgebase_mcp.mutations import canonical_json
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage import traversal
from justpen_knowledgebase_mcp.storage.traversal import _member_bytes, neighbors
from justpen_knowledgebase_mcp.storage.worker import OperationToken

from .graph_fixtures import admit

pytestmark = pytest.mark.integration


async def test_depth_direction_and_node_budget(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        result = await kb.write(
            WriteRequest.model_validate(
                {
                    "nodes": [
                        {"type": "domain", "properties": {"value": "example.com"}},
                        {"type": "subdomain", "properties": {"value": "a.example.com"}},
                        {"type": "subdomain", "properties": {"value": "b.a.example.com"}},
                        {"type": "subdomain", "properties": {"value": "c.b.a.example.com"}},
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
                            "type": "has_subdomain",
                            "source_ref": {"id": ids[i]},
                            "target_ref": {"id": ids[i + 1]},
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
        assert [node["id"] for node in incoming["nodes"]] == ids[:1]
        outgoing = await kb.neighbors(NeighborsRequest(seed_ids=[ids[0]], direction="out", depth=3))
        assert [node["id"] for node in outgoing["nodes"]] == ids
        assert len(outgoing["edges"]) == 3
        bounded = await kb.neighbors(NeighborsRequest(seed_ids=[ids[0]], depth=3, max_nodes=2))
        assert len(bounded["nodes"]) == 2
        assert bounded["truncated"]
        assert bounded["reason"] == "max_nodes"
        assert bounded["frontier"]


async def test_cycles_multiple_seeds_types_and_edge_budget(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        result = await kb.write(
            WriteRequest.model_validate(
                {"nodes": [{"type": "subdomain", "properties": {"value": f"h{i}.example.com"}} for i in range(3)]}
            )
        )
        ids = [item["id"] for item in result["nodes"]]
        await kb.write(
            WriteRequest.model_validate(
                {
                    "relations": [
                        {
                            "type": "cname_to",
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
        selected = await kb.neighbors(NeighborsRequest(seed_ids=ids[:1], relation_types=["cname_to"]))
        assert len(selected["edges"]) == 2
        with pytest.raises(InvalidParamsError):
            await kb.write(
                WriteRequest.model_validate(
                    {
                        "relations": [
                            {
                                "type": "has_subdomain",
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
                {"nodes": [{"type": "subdomain", "properties": {"value": f"h{i}.example.com"}} for i in range(100)]}
            )
        )
        ids = [item["id"] for item in result["nodes"]]
        await kb.write(
            WriteRequest.model_validate(
                {
                    "relations": [
                        {
                            "type": "cname_to",
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
            adjacency = []
            rows = []

            def trace(cursor, sql, bindings):
                if "from relations" in sql.lower():
                    adjacency.append(cursor)
                return True

            def trace_row(cursor, row):
                if any(cursor is stream for stream in adjacency):
                    rows.append(row)
                return row

            connection.set_exec_trace(trace)
            connection.set_row_trace(trace_row)
            try:
                result = neighbors(
                    connection, token, NeighborsRequest(seed_ids=ids[:1], max_nodes=2, relation_types=["cname_to"])
                )
            finally:
                connection.set_row_trace(None)
                connection.set_exec_trace(None)
            # Budget termination must close both live and exhausted streams.
            for cursor in adjacency:
                with pytest.raises(apsw.CursorClosedError):
                    cursor.get_description()
            return result, len(adjacency), rows

        bounded, query_count, rows = await kb.workers.read(run)
        assert query_count == 2  # One selective stream per direction, no per-edge restarts.
        assert len(rows) == 2  # One accepted edge and one proving the node budget is exhausted.
        assert all(row[2] == "cname_to" for row in rows)
        assert [node["id"] for node in bounded["nodes"]] == ids[:2]
        assert len(bounded["edges"]) == 1
        assert bounded["truncated"]
        assert bounded["reason"] == "max_nodes"
        assert bounded["frontier"] == ids[:1]

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
                            {"type": "subdomain", "properties": {"value": f"h{i}.example.com"}}
                            for i in range(start, start + 100)
                        ]
                    }
                )
            )
            ids.extend(item["id"] for item in result["nodes"])
        relations = [
            {
                "type": "has_mail_exchange",
                "source_ref": {"id": ids[0]},
                "target_ref": {"id": other},
                "properties": {"preference": vantage},
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
        # The running counter necessarily stops earlier than the whole-output check it replaced.
        # Pin how much earlier instead of letting the ceiling above absorb it: the accounted output
        # is still within one rejected edge, its endpoint node and one separator per array.
        accounted = len(canonical_json({**result, "frontier": []}).encode("utf-8"))
        headroom = max(map(_member_bytes, result["edges"])) + max(map(_member_bytes, result["nodes"])) + 2
        assert traversal.RESPONSE_BYTES - headroom <= accounted <= traversal.RESPONSE_BYTES


async def test_pending_visibility_matches_graph_readiness(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        result = await kb.write(
            WriteRequest.model_validate(
                {"nodes": [{"type": "subdomain", "properties": {"value": f"h{i}.example.com"}} for i in range(3)]}
            )
        )
        ids = [item["id"] for item in result["nodes"]]
        created = await kb.write(
            WriteRequest.model_validate(
                {
                    "relations": [
                        {
                            "type": "cname_to",
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
        assert len((await kb.search(SearchRequest(kind="nodes")))["items"]) == 3
        await admit(kb, "nodes", ids[2:])
        assert (await kb.neighbors(NeighborsRequest(seed_ids=ids[:1])))["edges"] == []
        assert (await kb.search(SearchRequest(kind="relations")))["items"] == []


def serialization_counter(monkeypatch, module):
    """Count bytes handed to canonical_json so cost is read as work, not as elapsed time."""
    total = [0]

    def counted(value):
        text = canonical_json(value)
        total[0] += len(text.encode("utf-8"))
        return text

    monkeypatch.setattr(module, "canonical_json", counted)
    return lambda: total[0]


async def test_edge_budget_serializes_each_appended_item_once(tmp_path, monkeypatch):
    """P3: the budget re-serialized the whole accumulated output on every appended edge, so the
    bytes serialized grew with the square of the edge count while the call count stayed linear.
    Serialized volume is therefore the observable that separates the two shapes; a wall-clock
    bound would measure the host instead."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        ids = []
        for start in range(0, 200, 100):
            written = await kb.write(
                WriteRequest.model_validate(
                    {
                        "nodes": [
                            {"type": "subdomain", "properties": {"value": f"h{index}.example.com"}}
                            for index in range(start, start + 100)
                        ]
                    }
                )
            )
            ids.extend(item["id"] for item in written["nodes"])
        relations = [
            {
                "type": "has_mail_exchange",
                "source_ref": {"id": ids[0]},
                "target_ref": {"id": other},
                "properties": {"preference": preference},
            }
            for other in ids[1:]
            for preference in range(3)
        ]
        for start in range(0, len(relations), 100):
            await kb.write(WriteRequest.model_validate({"relations": relations[start : start + 100]}))
        volumes, results = {}, {}
        for max_edges in (100, 300):
            serialized = serialization_counter(monkeypatch, traversal)
            results[max_edges] = await kb.workers.read(
                lambda connection, token, budget=max_edges: neighbors(
                    connection, token, NeighborsRequest(seed_ids=ids[:1], max_nodes=1000, max_edges=budget)
                )
            )
            volumes[max_edges] = serialized()
        assert [len(results[budget]["edges"]) for budget in (100, 300)] == [100, 300]
        # Three times the edges must not cost more than three times the serialization, with slack
        # for the fixed envelope; the replaced shape cost fifteen times as much here.
        assert volumes[300] <= 4 * volumes[100]
        assert volumes[300] <= 4 * len(canonical_json(results[300]).encode("utf-8"))
