"""Real SQLite exact search and projection consistency."""

import hashlib
import time
from typing import Any

import apsw
import pytest

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import InvalidParamsError, LimitError
from justpen_knowledgebase_mcp.evidence import IngestRequest
from justpen_knowledgebase_mcp.models import GetRequest, SearchRequest, WriteRequest
from justpen_knowledgebase_mcp.query import evaluate
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage import search as search_storage
from justpen_knowledgebase_mcp.storage.graph import row_by_id
from justpen_knowledgebase_mcp.storage.graph_sql import (
    EFFECTIVE_AUTHORIZATION,
    EFFECTIVE_OWNERSHIP,
    SCOPED_CHILD_RELATIONS,
)
from justpen_knowledgebase_mcp.storage.inventory import effective_state

from .graph_fixtures import evidence_fixture, inventory_graph, stated_request

pytestmark = pytest.mark.integration


async def test_search_partial_index_and_refresh(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        result = await kb.write(
            stated_request(
                {
                    "nodes": [
                        {
                            "type": "domain",
                            "properties": {"value": "a.example", "ports": list(range(600)), "status": 403},
                        }
                    ]
                }
            )
        )
        identifier = result["nodes"][0]["id"]
        indexed = await kb.search(SearchRequest(kind="nodes", properties={"path": "/status", "op": "eq", "value": 403}))
        assert [item["id"] for item in indexed["items"]] == [identifier]
        assert indexed["canonical_scan_count"] == 0
        fallback = await kb.search(
            SearchRequest(kind="nodes", properties={"path": "/ports/599", "op": "eq", "value": 599})
        )
        assert [item["id"] for item in fallback["items"]] == [identifier]
        assert fallback["canonical_scan_count"] == 1
        record = (await kb.get(GetRequest(kind="nodes", ids=[identifier])))["records"][0]
        assert len(record["properties"]["ports"]) == 600
        assert not record["property_index"]["complete"]
        await kb.write(stated_request({"nodes": [{"id": identifier, "remove_properties": ["/status"]}]}))
        assert not (
            await kb.search(SearchRequest(kind="nodes", properties={"path": "/status", "op": "exists", "value": True}))
        )["items"]


async def test_sql_matches_canonical_oracle_matrix(tmp_path):
    documents = [
        {"value": "a.example", "items": list(range(600)), "zzz_status_code": 403},
        {"value": "b.example", **{f"field{i:04}": i for i in range(600)}},
        {"value": "c.example", "items": {"00": None, "0": "403"}, "big": "é" * 513, "x" * 1024: "unindexed"},
        {"value": "d.example", "items": None, "big": "x" * 1024, "scalar": True},
        {"value": "e.example", "items": ["first"], "big": "x" * 1025, "scalar": 2**53 + 1},
    ]
    predicates = [
        {"path": path, "op": op, "value": value}
        for path in [
            "/items/599",
            "/items/00",
            "/items/0",
            "/field0599",
            "/big",
            "/scalar",
            "/" + "x" * 1024,
            "/items/0/child",
            "/zzz_status_code",
        ]
        for op, value in [
            ("eq", 403),
            ("ne", 403),
            ("exists", False),
            ("exists", True),
            ("eq", None),
            ("in", ["403", None, 599]),
            ("gt", 1),
            ("lte", "z"),
            ("eq", "é" * 513),
            ("ne", "x" * 1025),
        ]
    ]
    predicates += [
        {
            "all": [
                predicates[int.from_bytes(hashlib.sha256(f"5729:{index}".encode()).digest()) % len(predicates)],
                {
                    "any": [
                        predicates[
                            int.from_bytes(hashlib.sha256(f"5729:{index}:1".encode()).digest()) % len(predicates)
                        ],
                        predicates[
                            int.from_bytes(hashlib.sha256(f"5729:{index}:2".encode()).digest()) % len(predicates)
                        ],
                    ]
                },
            ]
        }
        for index in range(40)
    ]
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        result = await kb.write(
            stated_request({"nodes": [{"type": "domain", "properties": document} for document in documents]})
        )
        ids = [item["id"] for item in result["nodes"]]
        for predicate in predicates:
            expected = [
                identifier for identifier, document in zip(ids, documents, strict=True) if evaluate(document, predicate)
            ]
            actual = await kb.search(SearchRequest(kind="nodes", properties=predicate))
            assert [item["id"] for item in actual["items"]] == expected, predicate


async def test_storage_classes_sentinels_and_negative_affinity(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        await kb.write(
            stated_request(
                {
                    "nodes": [
                        {
                            "type": "domain",
                            "properties": {
                                "value": "a.example",
                                "string": "403",
                                "number": 403,
                                "boolean": True,
                                "null": None,
                                "big": "x" * 1025,
                            },
                        }
                    ]
                }
            )
        )

        def inspect(connection, token):
            rows = dict(connection.execute("SELECT path,typeof(value) FROM node_property_index"))
            connection.execute("CREATE TEMP TABLE affinity_negative(value NONE)")
            connection.execute("INSERT INTO affinity_negative VALUES (?)", ("403",))
            negative = connection.execute("SELECT typeof(value) FROM affinity_negative").get
            connection.execute("DROP TABLE affinity_negative")
            return rows, negative

        rows, negative = await kb.workers.write(inspect)
        assert negative == "integer"
        assert rows["/string"] == "text"
        assert rows["/number"] == rows["/boolean"] == "integer"
        assert rows["/null"] == rows["/big"] == "null"


async def test_cursor_write_stability_and_filter_binding(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        await kb.write(
            stated_request(
                {"nodes": [{"type": "domain", "properties": {"value": name + ".example"}} for name in ["a", "b", "c"]]}
            )
        )
        first = await kb.search(SearchRequest(kind="nodes", limit=1))
        await kb.write(stated_request({"nodes": [{"type": "domain", "properties": {"value": "d.example"}}]}))
        second = await kb.search(SearchRequest(kind="nodes", cursor=first["cursor"]))
        assert len(second["items"]) == 3
        with pytest.raises(InvalidParamsError):
            await kb.search(SearchRequest(kind="nodes", type="domain", cursor=first["cursor"]))


async def test_type_counts_use_type_index(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:

        def plan(connection, token):
            return list(
                connection.execute(
                    "EXPLAIN QUERY PLAN SELECT count(*) FROM relations WHERE type=? AND lifecycle='ready'",
                    ("cname_to",),
                )
            )

        assert any("relations_type_ready" in row[3] for row in await kb.workers.read(plan))


async def test_numeric_sql_precision_and_no_canonical_read_short_circuit(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        documents = [
            {
                "der_sha256": f"{index:064x}",
                "name": f"h{index}.example",
                "value": value,
                "large": "x" * 1025,
            }
            for index, value in enumerate([True, 1, 1.0, 2**53 + 1, float(2**53), None, "1", {}, -0.0])
        ]
        created = await kb.write(
            stated_request({"nodes": [{"type": "certificate", "properties": document} for document in documents]})
        )
        ids = [item["id"] for item in created["nodes"]]
        for operand in [True, 1, 1.0, 2**53 + 1, float(2**53), None, "1", 0]:
            predicate = {"path": "/value", "op": "eq", "value": operand}
            result = await kb.search(SearchRequest(kind="nodes", properties=predicate))
            assert [item["id"] for item in result["items"]] == [
                identifier for identifier, document in zip(ids, documents, strict=True) if evaluate(document, predicate)
            ]
            assert result["canonical_scan_count"] == 0
        for predicate in [
            {
                "all": [
                    {"path": "/large", "op": "eq", "value": "x" * 1025},
                    {"path": "/name", "op": "eq", "value": "absent"},
                ]
            },
            {
                "any": [
                    {"path": "/large", "op": "eq", "value": "different"},
                    {"path": "/name", "op": "exists", "value": True},
                ]
            },
        ]:
            result = await kb.search(SearchRequest(kind="nodes", properties=predicate))
            assert result["canonical_scan_count"] == 0


async def test_service_raw_mapping_presence_validation(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        assert (await kb.search({"kind": "nodes"}))["items"] == []
        with pytest.raises(InvalidParamsError):
            await kb.search({"kind": "nodes", "source_id": None})


async def test_fallback_expiry_is_limit_and_never_advances_cursor(tmp_path, monkeypatch):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        await kb.write(
            stated_request({"nodes": [{"type": "domain", "properties": {"value": "a.example", "large": "x" * 1025}}]})
        )
        predicate = {"path": "/large", "op": "eq", "value": "x" * 1025}

        def run(connection, token):
            def expire_after_evaluation(document, expression):
                result = evaluate(document, expression)
                token.deadline = time.monotonic() - 1
                return result

            monkeypatch.setattr(search_storage, "evaluate", expire_after_evaluation)
            return search_storage.search(connection, token, SearchRequest(kind="nodes", properties=predicate))

        with pytest.raises(LimitError):
            await kb.workers.read(run)


async def test_partial_materialized_predicate_does_not_read_canonical_body(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        await kb.write(
            stated_request(
                {
                    "nodes": [
                        {
                            "type": "domain",
                            "properties": {"value": "a.example", "ports": list(range(600)), "status": 403},
                        }
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
                result = search_storage.search(
                    connection,
                    token,
                    SearchRequest(kind="nodes", properties={"path": "/status", "op": "eq", "value": 403}),
                )
            finally:
                connection.set_exec_trace(None)
            return result, queries

        result, queries = await kb.workers.read(run)
        assert len(result["items"]) == 1
        assert not any("SELECT properties" in query for query in queries)


async def test_canonical_depth16_exists_depth17_is_missing(tmp_path):
    properties: dict[str, Any] = {"leaf": 42}
    for _ in range(15):
        properties = {"child": properties}
    properties["value"] = "a.example"
    path = "/child" * 15 + "/leaf"
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        await kb.write(stated_request({"nodes": [{"type": "domain", "properties": properties}]}))
        present = await kb.search(SearchRequest(kind="nodes", properties={"path": path, "op": "eq", "value": 42}))
        assert len(present["items"]) == 1
        missing = await kb.search(
            SearchRequest(kind="nodes", properties={"path": path + "/child", "op": "exists", "value": False})
        )
        assert len(missing["items"]) == 1
        assert missing["canonical_scan_count"] == 0


async def test_property_check_constraints_distinguish_null_and_sentinel(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        await kb.write(stated_request({"nodes": [{"type": "domain", "properties": {"value": "a.example"}}]}))
        for category, materialized, value in [
            ("null", 0, None),
            ("string", 1, 403),
            ("string", 0, "truncated"),
            ("boolean", 1, 2),
        ]:

            def insert_invalid(connection, token, category=category, materialized=materialized, value=value):
                with pytest.raises(apsw.ConstraintError):
                    connection.execute(
                        "INSERT INTO node_property_index(owner_id,path,value_type,value_materialized,value) VALUES(1,?,?,?,?)",
                        ("/invalid", category, materialized, value),
                    )

            await kb.workers.write(insert_invalid)


async def test_relation_properties_refresh_and_builtin_filters(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        written = await kb.write(
            stated_request(
                {
                    "nodes": [
                        {"type": "domain", "properties": {"value": "a.example"}},
                        {"type": "domain", "properties": {"value": "b.example"}},
                    ],
                    "relations": [
                        {
                            "type": "cname_to",
                            "source_ref": {"node_index": 0},
                            "target_ref": {"node_index": 1},
                            "properties": {"vantage": "test", "status": 403},
                            "source": "scanner",
                            "observed_at": "2026-09-15T00:00:00Z",
                        }
                    ],
                }
            )
        )
        identifier = written["relations"][0]["id"]
        result = await kb.search(
            SearchRequest(
                kind="relations",
                type="cname_to",
                source="scanner",
                source_id=written["nodes"][0]["id"],
                target_id=written["nodes"][1]["id"],
                first_seen_min="2026-09-14T00:00:00Z",
                last_seen_max="2026-09-16T00:00:00Z",
                properties={"path": "/status", "op": "eq", "value": 403},
            )
        )
        assert [item["id"] for item in result["items"]] == [identifier]
        assert result["canonical_scan_count"] == 0
        await kb.write(stated_request({"relations": [{"id": identifier, "remove_properties": ["/status"]}]}))
        assert not (
            await kb.search(
                SearchRequest(kind="relations", properties={"path": "/status", "op": "exists", "value": True})
            )
        )["items"]


async def test_seen_bounds_select_records_by_their_first_and_last_observation(tmp_path):
    """R12, KTD6: each bound is inclusive, and a node observed at T1 and T3 is seen at neither T2 bound."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        t1, t2, t3 = "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z"
        nodes = [
            {"type": "domain", "properties": {"value": "early.example"}, "observed_at": t1},
            {"type": "domain", "properties": {"value": "late.example"}, "observed_at": t3},
        ]
        early, late = [node["id"] for node in (await kb.write(stated_request({"nodes": nodes})))["nodes"]]
        await kb.write(stated_request({"nodes": [{**nodes[0], "observed_at": t3}]}))

        async def matches(**bounds: str) -> set[str]:
            request = SearchRequest.model_validate({"kind": "nodes", **bounds})
            return {item["id"] for item in (await kb.search(request))["items"]}

        assert await matches(last_seen_min=t3) == {early, late}
        assert await matches(first_seen_min=t2) == {late}
        assert await matches(first_seen_max=t1) == {early}
        assert await matches(last_seen_max=t2) == set()
        assert await matches(first_seen_min=t1, last_seen_max=t3) == {early, late}


async def test_relation_words_intersect_direct_and_single_linked_evidence_units(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        written = await kb.write(
            stated_request(
                {
                    "nodes": [
                        {"type": "domain", "properties": {"value": "first.example"}},
                        {"type": "domain", "properties": {"value": "second.example"}},
                    ],
                    "relations": [
                        {
                            "type": "cname_to",
                            "source_ref": {"node_index": 0},
                            "target_ref": {"node_index": 1},
                            "properties": {"vantage": "common", "second": "obscureneedle"},
                        },
                        {
                            "type": "cname_to",
                            "source_ref": {"node_index": 1},
                            "target_ref": {"node_index": 0},
                            "properties": {"vantage": "unrelated"},
                        },
                    ],
                }
            )
        )
        direct_id, linked_id = (relation["id"] for relation in written["relations"])
        target = [{"kind": "relations", "id": linked_id}]
        await kb.ingest_evidence(IngestRequest.model_validate({"text": "common only", "targets": target}))
        await kb.ingest_evidence(IngestRequest.model_validate({"text": "obscureneedle only", "targets": target}))
        query = SearchRequest(kind="relations", query="common obscureneedle", query_mode="words")
        assert [item["id"] for item in (await kb.search(query))["items"]] == [direct_id]
        await kb.ingest_evidence(
            IngestRequest.model_validate({"text": "common obscureneedle together", "targets": target})
        )
        assert [item["id"] for item in (await kb.search(query))["items"]] == [direct_id, linked_id]


async def test_maximum_accepted_ast_executes_with_exact_result(tmp_path):
    predicate = {"all": [{"path": "/value", "op": "in", "value": list(range(100))} for _ in range(32)]}
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        documents = [
            {"der_sha256": "a" * 64, "name": "a.example", "value": 42},
            {"der_sha256": "b" * 64, "name": "b.example", "value": "42"},
        ]
        written = await kb.write(
            stated_request({"nodes": [{"type": "certificate", "properties": document} for document in documents]})
        )
        result = await kb.search(SearchRequest(kind="nodes", properties=predicate))
        assert [item["id"] for item in result["items"]] == [
            item["id"]
            for item, document in zip(written["nodes"], documents, strict=True)
            if evaluate(document, predicate)
        ]
        assert result["canonical_scan_count"] == 0


async def test_empty_cursor_is_invalid_while_omitted_or_null_starts_first_page(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        written = await kb.write(stated_request({"nodes": [{"type": "domain", "properties": {"value": "a.example"}}]}))
        expected_ids = [written["nodes"][0]["id"]]
        for request in ({"kind": "nodes"}, {"kind": "nodes", "cursor": None}):
            result = await kb.search(request)
            assert [item["id"] for item in result["items"]] == expected_ids
        with pytest.raises(InvalidParamsError):
            await kb.search({"kind": "nodes", "cursor": ""})


async def state_matches(kb, **filters: object) -> set[str]:
    request = SearchRequest.model_validate({"kind": "nodes", "limit": 100, **filters})
    return {item["id"] for item in (await kb.search(request))["items"]}


async def test_authorization_filter_follows_narrowing_and_allowlists_down_the_scope_chain(tmp_path):
    """AE7, AE11, R14, R16, R27: one filtered search lists every in_scope asset, inherited ports,
    services and findings included, and leaves out what a narrowing or an allowlist excludes."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        ids = await inventory_graph(kb)
        in_scope = ("ip", "port_443", "https", "allowlisted", "port_80", "allowlisted_443", "http", "allowlisted_https")
        assert await state_matches(kb, authorization="in_scope") == {ids[name] for name in in_scope}
        out_of_scope = ("port_22", "ssh", "finding", "port_8080", "alternate")
        assert await state_matches(kb, authorization="out_of_scope") == {ids[name] for name in out_of_scope}
        assert await state_matches(kb, authorization="unknown") == {ids["candidate"], ids["www"]}


async def test_ownership_filter_and_default_search_keep_rejected_records_apart(tmp_path):
    """R25, Success Criteria: candidates come back without the rejected record, which only an
    explicit `rejected` filter returns; a default search leaves it out whatever it matches."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        ids = await inventory_graph(kb)
        assert await state_matches(kb, ownership="candidate") == {ids["candidate"], ids["www"]}
        assert await state_matches(kb, ownership="rejected") == {ids["stranger"]}
        assert ids["ssh"] in await state_matches(kb, ownership="owned")
        assert await state_matches(kb, type="domain") == {ids["candidate"]}
        assert ids["stranger"] not in await state_matches(kb)
        assert await state_matches(kb, query="stranger.example", include_evidence=False) == {ids["www"]}
        assert await state_matches(kb, query="stranger.example", ownership="rejected") == {ids["stranger"]}


async def test_search_summaries_carry_effective_state_and_the_inherited_root(tmp_path):
    """R14, KTD7: a scoped summary names its root; a stateless type reports no state."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        ids = await inventory_graph(kb)
        items = {item["id"]: item for item in (await kb.search(SearchRequest(kind="nodes", limit=100)))["items"]}
        ssh = items[ids["ssh"]]
        assert (ssh["ownership"], ssh["authorization"], ssh["state_root_id"]) == ("owned", "out_of_scope", ids["ip"])
        assert (items[ids["ip"]]["ownership"], items[ids["ip"]]["authorization"]) == ("owned", "in_scope")
        assert "state_root_id" not in items[ids["ip"]]
        assert not {"ownership", "authorization", "state_root_id"} & items[ids["advisory"]].keys()


# The statement shape a node search uses: its `scope_relation` table takes the first binding.
PROJECTED_STATE = (
    "WITH scope_relation(type) AS (SELECT value FROM json_each(?)) "
    "SELECT o.uuid,{ownership},{authorization},o.state_root_uuid FROM nodes o WHERE o.lifecycle='ready'"
)
# How the SQL form resolves each ready scoped node: from its root alone, or by walking its chain.
RESOLUTION_PATHS = (
    "SELECT DISTINCT CASE WHEN root.authorization='out_of_scope' THEN 'out_of_scope root' "
    "WHEN root.allowlist_scoped=1 THEN 'allowlist walk' WHEN EXISTS(SELECT 1 FROM nodes s "
    "WHERE s.state_root_uuid=root.uuid AND s.authorization_override IS NOT NULL) THEN 'override walk' "
    "ELSE 'inherited' END FROM nodes o JOIN nodes root ON root.uuid=o.state_root_uuid WHERE o.lifecycle='ready'"
)


async def inherited_roots(kb) -> None:
    """Write a port, service and finding chain under an in_scope, an unknown and an out_of_scope
    root; no scoped node overrides, so the SQL form takes each root's authorization directly."""
    evidence = (await evidence_fixture(kb, 1))[0]
    finding = {"rule": "nuclei:ssh-weak-cipher", "matcher": "", "title": "Weak SSH cipher", "severity": "low"}
    links = ("has_open_port", "has_service", "has_finding")
    nodes: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    for value, authorization in (("192.0.2.30", "in_scope"), ("192.0.2.40", "unknown"), ("192.0.2.50", "out_of_scope")):
        root = len(nodes)
        nodes.append(
            {
                "type": "ip_address",
                "properties": {"value": value, "version": 4},
                "ownership": "owned",
                "authorization": authorization,
                "evidence_add": [evidence],
            }
        )
        nodes.append({"type": "port", "properties": {"transport": "tcp", "number": 22}})
        nodes.append({"type": "service", "properties": {"name": "ssh"}})
        nodes.append({"type": "finding", "properties": finding})
        relations.extend(
            {
                "type": relation,
                "source_ref": {"node_index": root + depth},
                "target_ref": {"node_index": root + depth + 1},
                "properties": {},
            }
            for depth, relation in enumerate(links)
        )
    await kb.write(WriteRequest.model_validate({"nodes": nodes, "relations": relations}))


async def test_sql_effective_state_agrees_with_the_python_resolver(tmp_path):
    """KTD4: search filters on the SQL form and `kb_get` reports the Python walk; both must agree,
    whether the SQL form takes a root's authorization directly or walks the scope chain."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        await inventory_graph(kb)
        await inherited_roots(kb)

        def compare(connection, token):
            rows = [
                row_by_id(connection, "nodes", identifier)
                for (identifier,) in connection.execute("SELECT id FROM nodes WHERE lifecycle='ready'")
            ]
            walked = {}
            for row in rows:
                assert row is not None
                state = effective_state(connection, row)
                walked[row["uuid"]] = (state.ownership, state.authorization, state.root_id) if state else (None,) * 3
            projected = {
                uuid: (ownership, authorization, root)
                for uuid, ownership, authorization, root in connection.execute(
                    PROJECTED_STATE.format(ownership=EFFECTIVE_OWNERSHIP, authorization=EFFECTIVE_AUTHORIZATION),
                    (SCOPED_CHILD_RELATIONS,),
                )
            }
            paths = {path for (path,) in connection.execute(RESOLUTION_PATHS)}
            return walked, projected, paths

        walked, projected, paths = await kb.workers.read(compare)
        assert len(walked) == 29
        assert walked == projected
        assert paths == {"out_of_scope root", "allowlist walk", "override walk", "inherited"}
