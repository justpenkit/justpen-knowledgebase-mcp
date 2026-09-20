"""Real SQLite graph mutation and read contracts through admitted workers."""

import asyncio
import json
import sys
import time
from uuid import uuid4

import pytest

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import ConflictError, InvalidParamsError, NotFoundError, RecordConflictError
from justpen_knowledgebase_mcp.models import GetRequest, TypesRequest, WriteRequest
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.graph import _validate_endpoints, graph_types

from .graph_fixtures import admit, evidence_fixture, graph_node, scoped_stack

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("relation", "source", "target"),
    [
        (
            "has_subdomain",
            graph_node(1, "domain", {"value": "example.com"}),
            graph_node(2, "subdomain", {"value": "api.dev.example.com"}),
        ),
        (
            "has_subdomain",
            graph_node(1, "subdomain", {"value": "dev.example.com"}),
            graph_node(2, "subdomain", {"value": "api.dev.example.com"}),
        ),
        (
            "contains_ip",
            graph_node(1, "ip_cidr", {"value": "192.0.2.0/24", "version": 4}),
            graph_node(2, "ip_address", {"value": "192.0.2.0", "version": 4}),
        ),
        (
            "contains_ip",
            graph_node(1, "ip_cidr", {"value": "192.0.2.0/24", "version": 4}),
            graph_node(2, "ip_address", {"value": "192.0.2.255", "version": 4}),
        ),
        (
            "contains_cidr",
            graph_node(1, "ip_cidr", {"value": "10.0.0.0/8", "version": 4}),
            graph_node(2, "ip_cidr", {"value": "10.2.3.0/24", "version": 4}),
        ),
    ],
)
def test_catalog_v2_structural_endpoints_accept_locked_relationships(
    relation: str, source: dict[str, object], target: dict[str, object]
) -> None:
    _validate_endpoints(relation, source, target)


@pytest.mark.parametrize(
    ("relation", "source", "target"),
    [
        (
            "has_subdomain",
            graph_node(1, "domain", {"value": "example.com"}),
            graph_node(2, "subdomain", {"value": "fakeexample.com"}),
        ),
        (
            "has_subdomain",
            graph_node(1, "subdomain", {"value": "api.example.com"}),
            graph_node(2, "subdomain", {"value": "api.example.com"}),
        ),
        (
            "contains_ip",
            graph_node(1, "ip_cidr", {"value": "192.0.2.0/24", "version": 4}),
            graph_node(2, "ip_address", {"value": "192.0.3.1", "version": 4}),
        ),
        (
            "contains_ip",
            graph_node(1, "ip_cidr", {"value": "192.0.2.0/24", "version": 4}),
            graph_node(2, "ip_address", {"value": "2001:db8::1", "version": 6}),
        ),
        (
            "contains_cidr",
            graph_node(1, "ip_cidr", {"value": "10.0.0.0/8", "version": 4}),
            graph_node(2, "ip_cidr", {"value": "10.0.0.0/8", "version": 4}),
        ),
        (
            "contains_cidr",
            graph_node(1, "ip_cidr", {"value": "10.0.0.0/8", "version": 4}),
            graph_node(2, "ip_cidr", {"value": "2001:db8::/32", "version": 6}),
        ),
        (
            "contains_cidr",
            graph_node(1, "ip_cidr", {"value": "10.0.0.0/16", "version": 4}),
            graph_node(2, "ip_cidr", {"value": "10.0.0.0/8", "version": 4}),
        ),
    ],
)
def test_catalog_v2_structural_endpoints_reject_invalid_relationships(
    relation: str, source: dict[str, object], target: dict[str, object]
) -> None:
    with pytest.raises(InvalidParamsError, match="relation endpoint constraint failed"):
        _validate_endpoints(relation, source, target)


def write(value):
    return WriteRequest.model_validate(value)


async def test_scoped_batch_order_independent_and_same_parent_deduplicates(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        first = await kb.write(write(scoped_stack()))
        first_ids = {
            name: result["id"] for name, result in zip(("ip_address", "port", "service"), first["nodes"], strict=True)
        }
        second_order = ("service", "port", "ip_address")
        second = await kb.write(write(scoped_stack(second_order, reverse_relations=True)))
        second_ids = {name: result["id"] for name, result in zip(second_order, second["nodes"], strict=True)}
        assert second_ids == first_ids
        assert await kb.workers.read(lambda c, t: c.execute("select count(*) from nodes").get) == 3
        assert await kb.workers.read(lambda c, t: c.execute("select count(*) from relations").get) == 2


@pytest.mark.parametrize("scope_count", [0, 2])
async def test_new_scoped_node_requires_exactly_one_scope_relation(tmp_path, scope_count):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        relations = [
            {
                "type": "has_open_port",
                "source_ref": {"node_index": source},
                "target_ref": {"node_index": 2},
                "properties": {},
            }
            for source in range(scope_count)
        ]
        nodes = [
            {"type": "ip_address", "properties": {"value": "192.0.2.10", "version": 4}},
            {"type": "ip_address", "properties": {"value": "192.0.2.11", "version": 4}},
            {"type": "port", "properties": {"transport": "tcp", "number": 443}},
        ]
        with pytest.raises(InvalidParamsError, match="exactly one has_open_port"):
            await kb.write(write({"nodes": nodes, "relations": relations}))
        assert await kb.workers.read(lambda c, t: c.execute("select count(*) from nodes").get) == 0


async def test_scoped_write_failure_has_zero_partial_rows(tmp_path):
    request = scoped_stack()
    request["relations"].append(
        {
            "type": "contains_ip",
            "source_ref": {"node_index": 0},
            "target_ref": {"node_index": 0},
            "properties": {},
        }
    )
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        with pytest.raises(InvalidParamsError, match="endpoint types"):
            await kb.write(write(request))
        assert await kb.workers.read(lambda c, t: c.execute("select count(*) from nodes").get) == 0
        assert await kb.workers.read(lambda c, t: c.execute("select count(*) from relations").get) == 0


async def test_existing_scoped_node_cannot_be_reparented_and_patch_needs_no_scope_relation(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        created = await kb.write(write(scoped_stack(("ip_address", "port"))))
        first_parent, port = [item["id"] for item in created["nodes"]]
        second_parent = (
            await kb.write(
                write({"nodes": [{"type": "ip_address", "properties": {"value": "192.0.2.11", "version": 4}}]})
            )
        )["nodes"][0]["id"]
        await kb.write(write({"nodes": [{"id": port, "properties": {"banner": "updated"}}]}))
        with pytest.raises(InvalidParamsError, match="different parent"):
            await kb.write(
                write(
                    {
                        "relations": [
                            {
                                "type": "has_open_port",
                                "source_ref": {"id": second_parent},
                                "target_ref": {"id": port},
                                "properties": {},
                            }
                        ]
                    }
                )
            )
        record = (await kb.get(GetRequest(kind="nodes", ids=[port])))["records"][0]
        assert record["properties"]["banner"] == "updated"
        relation = await kb.types(TypesRequest(kind="nodes", type="port"))
        assert relation["types"][0]["identity"] == {
            "properties": ["transport", "number"],
            "scope": {"relation": "has_open_port", "endpoint": "source"},
        }
        assert relation["types"][0]["properties_schema"]["x-identity"] == relation["types"][0]["identity"]
        assert first_parent != second_parent


async def test_identity_upsert_atomic_batch_and_metadata_presence(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        result = await kb.write(
            write(
                {
                    "nodes": [
                        {
                            "type": "certificate",
                            "properties": {"der_sha256": "a" * 64, "platform": "android", "nested": {"a": 1}},
                            "label": "App",
                        }
                    ]
                }
            )
        )
        identifier = result["nodes"][0]["id"]
        updated = await kb.write(
            write(
                {
                    "nodes": [
                        {
                            "type": "certificate",
                            "properties": {"der_sha256": "a" * 64, "platform": "linux", "nested": {"b": 2}},
                        }
                    ]
                }
            )
        )
        assert updated["nodes"][0]["id"] == identifier
        await kb.write(
            write(
                {
                    "nodes": [
                        {
                            "id": identifier,
                            "source": None,
                            "observed_at": "2001-01-01T00:00:00Z",
                            "properties": {"old": True},
                        }
                    ]
                }
            )
        )
        record = (await kb.get(GetRequest(kind="nodes", ids=[identifier])))["records"][0]
        assert record["label"] == "App"
        assert record["properties"]["nested"] == {"a": 1, "b": 2}
        assert record["observed_at"] == "2001-01-01T00:00:00.000000Z"
        with pytest.raises(InvalidParamsError):
            await kb.write(
                write(
                    {
                        "nodes": [
                            {"type": "ip_address", "properties": {"value": "192.0.2.1", "version": 4}},
                            {"type": "bad", "properties": {}},
                        ]
                    }
                )
            )
        assert await kb.workers.read(lambda c, t: c.execute("select count(*) from nodes").get) == 1
        with pytest.raises(ConflictError):
            await kb.write(write({"nodes": [{"id": identifier, "properties": {"der_sha256": "b" * 64}}]}))
        with pytest.raises(NotFoundError):
            await kb.write(write({"nodes": [{"id": str(uuid4())}]}))


async def test_shared_node_relations_and_duplicate_alias_rejection(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        result = await kb.write(
            write(
                {
                    "nodes": [
                        {"type": "domain", "properties": {"value": "example.com"}},
                        {"type": "domain", "properties": {"value": "example.net"}},
                        {"type": "ip_address", "properties": {"value": "192.0.2.1", "version": 4}},
                    ],
                    "relations": [
                        {
                            "type": "resolves_to",
                            "source_ref": {"node_index": i},
                            "target_ref": {"node_index": 2},
                            "properties": {"vantage": "public"},
                        }
                        for i in (0, 1)
                    ],
                }
            )
        )
        shared_ip = result["nodes"][2]["id"]
        assert len(result["relations"]) == 2
        with pytest.raises(InvalidParamsError):
            await kb.write(
                write(
                    {
                        "nodes": [
                            {"id": shared_ip},
                            {"type": "ip_address", "properties": {"value": "192.0.2.1", "version": 4}},
                        ]
                    }
                )
            )
        relation = result["relations"][0]["id"]
        await kb.write(write({"relations": [{"id": relation, "properties": {"note": "new"}}]}))
        assert (await kb.get(GetRequest(kind="relations", ids=[relation])))["records"][0]["properties"]["note"] == "new"


async def test_get_budgets_keep_whole_records_and_missing_ids(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        result = await kb.write(
            write(
                {
                    "nodes": [
                        {
                            "type": "subdomain",
                            "properties": {"value": f"h{i}.example.com", "extra": "x" * 60000},
                        }
                        for i in range(5)
                    ]
                }
            )
        )
        identifiers = [record["id"] for record in result["nodes"]]
        missing = str(uuid4())
        output = await kb.get(GetRequest(kind="nodes", ids=[*identifiers, missing]))
        assert len(output["records"]) == 4
        assert output["remaining_ids"] == [identifiers[4]]
        assert output["missing_ids"] == [missing]
        assert all(len(record["properties"]["extra"]) == 60000 for record in output["records"])


async def test_unlimited_lifetime_links_and_owner_bound_pagination(tmp_path):

    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = await evidence_fixture(kb, 150)
        result = await kb.write(
            write(
                {
                    "nodes": [
                        {
                            "type": "ip_address",
                            "properties": {"value": "192.0.2.1", "version": 4},
                            "evidence_add": evidence[:100],
                        }
                    ]
                }
            )
        )
        identifier = result["nodes"][0]["id"]
        await kb.write(write({"nodes": [{"id": identifier, "evidence_add": evidence[100:]}]}))
        record = (await kb.get(GetRequest(kind="nodes", ids=[identifier])))["records"][0]
        assert record["link_count"] == 150
        first = await kb.get(GetRequest(kind="nodes", ids=[identifier], view="links", limit=100))
        second = await kb.get(
            GetRequest(kind="nodes", ids=[identifier], view="links", limit=100, cursor=first["next_cursor"])
        )
        assert len(first["links"]) == 100
        assert len(second["links"]) == 50
        assert set(first["links"] + second["links"]) == set(evidence)
        assert second["next_cursor"] is None
        with pytest.raises(InvalidParamsError):
            await kb.get(GetRequest(kind="evidence", ids=[evidence[0]], view="links", cursor=first["next_cursor"]))
        targets = await kb.get(GetRequest(kind="evidence", ids=[evidence[0]], view="links"))
        assert targets["links"] == [{"kind": "nodes", "id": identifier}]

        def sources(connection, token):
            owner = connection.execute("select id from evidence where uuid=?", (evidence[0],)).get
            connection.executemany(
                "insert into evidence_sources(evidence_id,source,first_seen_at,last_seen_at) values (?,?,?,?)",
                [(owner, f"s{i}", "first", "last") for i in range(21)],
            )

        await kb.workers.write(sources)
        source_page = await kb.get(GetRequest(kind="evidence", ids=[evidence[0]], view="sources"))
        assert len(source_page["sources"]) == 20
        assert source_page["next_cursor"]
        types = await kb.types(TypesRequest(kind="nodes"))
        assert len(types["types"]) == 16
        assert {item["type"]: item["count"] for item in types["types"]}["ip_address"] == 1
        assert {item["type"]: item["count"] for item in types["types"]}["endpoint"] == 0


async def test_evidence_link_cursor_preserves_colliding_association_ids(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = (await evidence_fixture(kb, 1))[0]
        output = await kb.write(
            write(
                {
                    "nodes": [
                        {"type": "domain", "properties": {"value": "example.com"}, "evidence_add": [evidence]},
                        {"type": "ip_address", "properties": {"value": "192.0.2.1", "version": 4}},
                    ],
                    "relations": [
                        {
                            "type": "resolves_to",
                            "source_ref": {"node_index": 0},
                            "target_ref": {"node_index": 1},
                            "properties": {"vantage": "public"},
                            "evidence_add": [evidence],
                        }
                    ],
                }
            )
        )
        first = await kb.get(GetRequest(kind="evidence", ids=[evidence], view="links", limit=1))
        second = await kb.get(
            GetRequest(kind="evidence", ids=[evidence], view="links", limit=1, cursor=first["next_cursor"])
        )
        assert first["links"] == [{"kind": "nodes", "id": output["nodes"][0]["id"]}]
        assert second["links"] == [{"kind": "relations", "id": output["relations"][0]["id"]}]


@pytest.mark.parametrize("mode", ["disjoint", "shared", "same"])
async def test_two_process_updates_and_shared_identity(tmp_path, mode):

    script = r"""
import asyncio,json,sys
from pathlib import Path
from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.models import WriteRequest
from justpen_knowledgebase_mcp.service import KnowledgeBase
async def run():
    async with KnowledgeBase.open(ServerConfig(workspace_dir=Path(sys.argv[1]))) as kb:
        print("ready",flush=True)
        request=json.loads(await asyncio.to_thread(sys.stdin.readline))
        print(json.dumps(await kb.write(WriteRequest.model_validate(request))),flush=True)
asyncio.run(run())
"""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        created = await kb.write(write({"nodes": [{"type": "domain", "properties": {"value": "example.com"}}]}))
        identifier = created["nodes"][0]["id"]
        children = [
            await asyncio.create_subprocess_exec(
                sys.executable,
                "-B",
                "-c",
                script,
                str(tmp_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            for _ in range(2)
        ]
        try:
            for child in children:
                assert child.stdout is not None
                assert await asyncio.wait_for(child.stdout.readline(), 15) == b"ready\n"
            payloads = [
                {"nodes": [{"id": identifier, "properties": {field: number}}]}
                for field, number in [("left", 1), ("right", 2)]
            ]
            if mode == "shared":
                payloads = [
                    {
                        "nodes": [
                            {"type": "subdomain", "properties": {"value": f"a{digit}.example.com"}},
                            {"type": "ip_address", "properties": {"value": "192.0.2.1", "version": 4}},
                        ],
                        "relations": [
                            {
                                "type": "resolves_to",
                                "source_ref": {"node_index": 0},
                                "target_ref": {"node_index": 1},
                                "properties": {"vantage": "public"},
                            }
                        ],
                    }
                    for digit in ("a", "b")
                ]
            if mode == "same":
                payloads = [
                    {"nodes": [{"id": identifier, "properties": {"same": value}, "observed_at": timestamp}]}
                    for value, timestamp in ((1, "2026-01-01T00:00:00Z"), (2, "2001-01-01T00:00:00Z"))
                ]
                outputs = [
                    await asyncio.wait_for(child.communicate(json.dumps(payload).encode()), 15)
                    for child, payload in zip(children, payloads, strict=True)
                ]
            else:
                outputs = await asyncio.wait_for(
                    asyncio.gather(
                        *(
                            child.communicate(json.dumps(payload).encode())
                            for child, payload in zip(children, payloads, strict=True)
                        )
                    ),
                    15,
                )
            assert all(child.returncode == 0 for child in children), outputs
        finally:
            for child in children:
                if child.returncode is None:
                    child.kill()
                await asyncio.wait_for(child.wait(), 5)
        record = (await kb.get(GetRequest(kind="nodes", ids=[identifier])))["records"][0]
        if mode == "disjoint":
            assert record["properties"] == {"value": "example.com", "left": 1, "right": 2}
        elif mode == "same":
            assert record["properties"]["same"] == 2
            assert record["observed_at"] == "2001-01-01T00:00:00.000000Z"
        else:
            assert (
                await kb.workers.read(lambda c, t: c.execute("select count(*) from nodes where type='ip_address'").get)
                == 1
            )
            assert await kb.workers.read(lambda c, t: c.execute("select count(*) from relations").get) == 2
        # A controlled commit order on the same field preserves the final writer's payload and timestamp.
        for value in (1, 2):
            await kb.write(
                write(
                    {
                        "nodes": [
                            {
                                "id": identifier,
                                "properties": {"same": value},
                                "observed_at": f"200{value}-01-01T00:00:00Z",
                            }
                        ]
                    }
                )
            )
        assert (await kb.get(GetRequest(kind="nodes", ids=[identifier])))["records"][0]["properties"]["same"] == 2


async def test_ready_only_counts_pending_evidence_and_deferred_counts(tmp_path):

    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = (await evidence_fixture(kb, 1))[0]
        output = await kb.write(
            write(
                {
                    "nodes": [
                        {"type": "domain", "properties": {"value": "example.com"}},
                        {"type": "ip_address", "properties": {"value": "192.0.2.1", "version": 4}},
                    ],
                    "relations": [
                        {
                            "type": "resolves_to",
                            "source_ref": {"node_index": 0},
                            "target_ref": {"node_index": 1},
                            "properties": {"vantage": "public"},
                        }
                    ],
                }
            )
        )
        await admit(kb, "nodes", [output["nodes"][0]["id"]])
        counted = await kb.types(TypesRequest(kind="relations", type="resolves_to"))
        assert counted["types"][0]["count"] == 0
        await admit(kb, "evidence", [evidence])
        with pytest.raises(RecordConflictError, match="RECORD_DELETING") as caught:
            await kb.write(write({"nodes": [{"id": output["nodes"][1]["id"], "evidence_add": [evidence]}]}))
        assert caught.value.details.blocking_record.kind == "evidence"

        def deferred(connection, token):
            token.deadline = time.monotonic() + 0.05
            return graph_types(connection, token, TypesRequest(kind="nodes"))

        result = await kb.workers.read(deferred)
        assert result["counts_deferred"]
        assert all(item["count"] is None for item in result["types"])


async def test_metadata_is_integer_and_association_ids_are_not_reused(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = (await evidence_fixture(kb, 1))[0]
        output = await kb.write(
            write(
                {
                    "nodes": [
                        {
                            "type": "ip_address",
                            "properties": {"value": "192.0.2.1", "version": 4},
                            "observed_at": "1970-01-01T00:00:00.000001Z",
                            "evidence_add": [evidence],
                        }
                    ]
                }
            )
        )
        identifier = output["nodes"][0]["id"]
        assert await kb.workers.read(
            lambda c, t: c.execute("select observed_at,typeof(observed_at) from nodes").get
        ) == (1, "integer")
        first = await kb.workers.read(lambda c, t: c.execute("select id from node_evidence").get)
        await kb.write(write({"nodes": [{"id": identifier, "evidence_remove": [evidence]}]}))
        await kb.write(write({"nodes": [{"id": identifier, "evidence_add": [evidence]}]}))
        assert await kb.workers.read(lambda c, t: c.execute("select id from node_evidence").get) > first


async def test_identity_relation_pending_prefers_own_intent_before_endpoint(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        output = await kb.write(
            write(
                {
                    "nodes": [
                        {"type": "domain", "properties": {"value": "example.com"}},
                        {"type": "ip_address", "properties": {"value": "192.0.2.1", "version": 4}},
                    ],
                    "relations": [
                        {
                            "type": "resolves_to",
                            "source_ref": {"node_index": 0},
                            "target_ref": {"node_index": 1},
                            "properties": {"vantage": "public"},
                        }
                    ],
                }
            )
        )
        relation = output["relations"][0]["id"]
        source, target = [row["id"] for row in output["nodes"]]
        own = (await admit(kb, "relations", [relation]))[0]
        await admit(kb, "nodes", [source])
        with pytest.raises(RecordConflictError) as caught:
            await kb.write(
                write(
                    {
                        "relations": [
                            {
                                "type": "resolves_to",
                                "source_ref": {"id": source},
                                "target_ref": {"id": target},
                                "properties": {"vantage": "public"},
                            }
                        ]
                    }
                )
            )
        assert str(caught.value.details.delete_job_id) == own.job_id
        assert caught.value.details.blocking_record.kind == "relations"


async def test_upsert_missing_remove_and_required_identity_rules(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        request = write(
            {"nodes": [{"type": "domain", "properties": {"value": "example.com"}, "remove_properties": ["/old"]}]}
        )
        first = await kb.write(request)
        second = await kb.write(request)
        identifier = first["nodes"][0]["id"]
        assert second["nodes"][0]["id"] == identifier
        with pytest.raises(InvalidParamsError):
            await kb.write(write({"nodes": [{"id": identifier, "remove_properties": ["/value"]}]}))
        with pytest.raises(InvalidParamsError):
            await kb.write(write({"nodes": [{"id": identifier, "properties": {"a": 1}, "remove_properties": ["/a"]}]}))
        assert (await kb.get(GetRequest(kind="nodes", ids=[identifier])))["records"][0]["properties"] == {
            "value": "example.com"
        }


@pytest.mark.parametrize(
    ("relation", "source", "target", "properties"),
    [
        (
            "has_subdomain",
            {"type": "domain", "properties": {"value": "example.com"}},
            {"type": "subdomain", "properties": {"value": "api.other.com"}},
            {},
        ),
        (
            "contains_ip",
            {"type": "ip_cidr", "properties": {"value": "192.0.2.0/24", "version": 4}},
            {"type": "ip_address", "properties": {"value": "192.0.3.1", "version": 4}},
            {},
        ),
        (
            "contains_cidr",
            {"type": "ip_cidr", "properties": {"value": "192.0.2.0/24", "version": 4}},
            {"type": "ip_cidr", "properties": {"value": "192.0.0.0/16", "version": 4}},
            {},
        ),
    ],
)
async def test_endpoint_cross_field_constraints_rollback_batch(tmp_path, relation, source, target, properties):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        with pytest.raises(InvalidParamsError):
            await kb.write(
                write(
                    {
                        "nodes": [source, target],
                        "relations": [
                            {
                                "type": relation,
                                "source_ref": {"node_index": 0},
                                "target_ref": {"node_index": 1},
                                "properties": properties,
                            }
                        ],
                    }
                )
            )
        assert await kb.workers.read(lambda c, t: c.execute("select count(*) from nodes").get) == 0


async def test_cname_cycles_and_self_edges_are_preserved(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        result = await kb.write(
            write(
                {
                    "nodes": [
                        {"type": "domain", "properties": {"value": name}} for name in ("example.com", "example.net")
                    ],
                    "relations": [
                        {
                            "type": "cname_to",
                            "source_ref": {"node_index": source},
                            "target_ref": {"node_index": target},
                            "properties": {},
                        }
                        for source, target in ((0, 1), (1, 0))
                    ],
                }
            )
        )
        assert len(result["relations"]) == 2
        self_edge = await kb.write(
            write(
                {
                    "relations": [
                        {
                            "type": "cname_to",
                            "source_ref": {"id": result["nodes"][0]["id"]},
                            "target_ref": {"id": result["nodes"][0]["id"]},
                            "properties": {},
                        }
                    ]
                }
            )
        )
        assert self_edge["relations"][0]["created"]
