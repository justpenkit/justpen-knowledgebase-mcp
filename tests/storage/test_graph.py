"""Real SQLite graph mutation and read contracts through admitted workers."""

import asyncio
import json
import sys
import time
from uuid import uuid4

import pytest

import justpen_knowledgebase_mcp.catalog as catalog_module
from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import (
    ConflictError,
    ExpectedValidationError,
    InvalidParamsError,
    NotFoundError,
    RecordConflictError,
    RejectedIdentityError,
)
from justpen_knowledgebase_mcp.identity import format_timestamp, parse_timestamp
from justpen_knowledgebase_mcp.models import GetRequest, SearchRequest, TypesRequest, WriteRequest
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.graph import (
    _RELIANCE_RELATIONS,
    _validate_endpoint_values,
    _validate_endpoints,
    graph_types,
    row_by_id,
)
from justpen_knowledgebase_mcp.storage.inventory import effective_state

from .graph_fixtures import admit, evidence_fixture, graph_node, inventory_graph, scoped_stack, stated

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
def test_structural_endpoints_accept_locked_relationships(
    relation: str, source: dict[str, object], target: dict[str, object]
) -> None:
    _validate_endpoints(relation, source, target)
    _validate_endpoint_values(relation, {}, source, target)


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
def test_structural_endpoints_reject_invalid_relationships(
    relation: str, source: dict[str, object], target: dict[str, object]
) -> None:
    _validate_endpoints(relation, source, target)
    with pytest.raises(ExpectedValidationError, match="relation endpoint constraint failed"):
        _validate_endpoint_values(relation, {}, source, target)


def write(value):
    return WriteRequest.model_validate(stated(value))


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


async def test_dkim_and_parameter_scope_to_their_parent_and_separate_by_parent(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        request = {
            "nodes": [
                {"type": "domain", "properties": {"value": "example.com"}},
                {"type": "domain", "properties": {"value": "example.net"}},
                {"type": "dkim_record", "properties": {"selector": "default", "value": "v=DKIM1; p=A"}},
                {"type": "dkim_record", "properties": {"selector": "default", "value": "v=DKIM1; p=B"}},
                {"type": "endpoint", "properties": {"url": "https://example.com/a", "method": "GET"}},
                {"type": "endpoint", "properties": {"url": "https://example.com/b", "method": "GET"}},
                {"type": "parameter", "properties": {"name": "id", "location": "query"}},
                {"type": "parameter", "properties": {"name": "id", "location": "query"}},
            ],
            "relations": [
                {
                    "type": "has_dkim_selector",
                    "source_ref": {"node_index": 0},
                    "target_ref": {"node_index": 2},
                    "properties": {},
                },
                {
                    "type": "has_dkim_selector",
                    "source_ref": {"node_index": 1},
                    "target_ref": {"node_index": 3},
                    "properties": {},
                },
                {
                    "type": "has_parameter",
                    "source_ref": {"node_index": 4},
                    "target_ref": {"node_index": 6},
                    "properties": {},
                },
                {
                    "type": "has_parameter",
                    "source_ref": {"node_index": 5},
                    "target_ref": {"node_index": 7},
                    "properties": {},
                },
            ],
        }
        created = await kb.write(write(request))
        identifiers = [node["id"] for node in created["nodes"]]
        assert identifiers[2] != identifiers[3]
        assert identifiers[6] != identifiers[7]
        assert await kb.workers.read(lambda c, t: c.execute("select count(*) from nodes").get) == 8


@pytest.mark.parametrize(
    ("child", "properties", "parent", "parent_properties", "relation"),
    [
        (
            "dkim_record",
            {"selector": "default", "value": "v=DKIM1; p=A"},
            "domain",
            {"value": "example.com"},
            "has_dkim_selector",
        ),
        (
            "parameter",
            {"name": "id", "location": "query"},
            "endpoint",
            {"url": "https://example.com/a", "method": "GET"},
            "has_parameter",
        ),
    ],
)
async def test_new_dkim_and_parameter_nodes_require_their_scope_relation(
    tmp_path, child, properties, parent, parent_properties, relation
):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        nodes = [{"type": parent, "properties": parent_properties}, {"type": child, "properties": properties}]
        with pytest.raises(InvalidParamsError, match=f"exactly one {relation}"):
            await kb.write(write({"nodes": nodes}))
        assert await kb.workers.read(lambda c, t: c.execute("select count(*) from nodes").get) == 0
        created = await kb.write(
            write(
                {
                    "nodes": nodes,
                    "relations": [
                        {
                            "type": relation,
                            "source_ref": {"node_index": 0},
                            "target_ref": {"node_index": 1},
                            "properties": {},
                        }
                    ],
                }
            )
        )
        second_parent = (
            await kb.write(write({"nodes": [{"type": "domain", "properties": {"value": "example.net"}}]}))
        )["nodes"][0]["id"]
        if child == "dkim_record":
            with pytest.raises(InvalidParamsError, match="different parent"):
                await kb.write(
                    write(
                        {
                            "relations": [
                                {
                                    "type": relation,
                                    "source_ref": {"id": second_parent},
                                    "target_ref": {"id": created["nodes"][1]["id"]},
                                    "properties": {},
                                }
                            ]
                        }
                    )
                )


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
        # An ID write observed before the stored last seen lowers first seen and only adds `old`.
        assert record["properties"]["old"] is True
        assert record["first_seen"] == "2001-01-01T00:00:00.000000Z"
        assert record["last_seen"] > record["first_seen"]
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
        assert len(types["types"]) == 20
        assert types["next_cursor"]
        rest = await kb.types(TypesRequest(kind="nodes", cursor=types["next_cursor"]))
        assert rest["next_cursor"] is None
        counts = {item["type"]: item["count"] for item in [*types["types"], *rest["types"]]}
        assert len(counts) == len(catalog_module.catalog_manifest()["nodes"])
        assert counts["ip_address"] == 1
        assert counts["endpoint"] == 0


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
        domain = {"type": "domain", "properties": {"value": "example.com"}, "observed_at": "2000-01-01T00:00:00Z"}
        created = await kb.write(write({"nodes": [domain]}))
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
                payloads = [stated(payload) for payload in payloads]
            if mode == "same":
                # Either commit order ends the same way: the later observation's value, and both bounds.
                observations = ((2, "2002-01-01T00:00:00Z"), (1, "2001-01-01T00:00:00Z"))
                payloads = [
                    {"nodes": [{"id": identifier, "properties": {"same": value}, "observed_at": observed_at}]}
                    for value, observed_at in observations
                ]
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
            assert (record["first_seen"], record["last_seen"]) == (
                "2000-01-01T00:00:00.000000Z",
                "2002-01-01T00:00:00.000000Z",
            )
        else:
            assert (
                await kb.workers.read(lambda c, t: c.execute("select count(*) from nodes where type='ip_address'").get)
                == 1
            )
            assert await kb.workers.read(lambda c, t: c.execute("select count(*) from relations").get) == 2
        # A controlled commit order that delivers the newer observation first keeps its value and bounds.
        # The newer one is the current time: an `observed_at` past the server's clock is refused.
        newest = format_timestamp(time.time_ns() // 1000)
        for value, observed_at in ((3, newest), (0, "1990-01-01T00:00:00Z")):
            patch = {"id": identifier, "properties": {"same": value}, "observed_at": observed_at}
            await kb.write(write({"nodes": [patch]}))
        record = (await kb.get(GetRequest(kind="nodes", ids=[identifier])))["records"][0]
        assert record["properties"]["same"] == 3
        assert (record["first_seen"], record["last_seen"]) == ("1990-01-01T00:00:00.000000Z", newest)


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
            lambda c, t: c.execute("select first_seen,last_seen,typeof(first_seen),typeof(last_seen) from nodes").get
        ) == (1, 1, "integer", "integer")
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


async def test_endpoint_values_are_checked_on_the_properties_that_will_be_stored(tmp_path, monkeypatch):
    """The value check runs after both merges: an id patch that omits a property still sees the
    stored one, and a keyless rewrite that deduplicates onto a stored edge sees the merged whole."""
    seen: list[dict[str, object]] = []
    run = catalog_module._ENDPOINT_CHECKS["has_subdomain_suffix.1"]

    def recording(relation_props, source, target):
        seen.append(dict(relation_props))
        run(relation_props, source, target)

    monkeypatch.setitem(catalog_module._ENDPOINT_CHECKS, "has_subdomain_suffix.1", recording)
    nodes = [
        {"type": "domain", "properties": {"value": "example.com"}},
        {"type": "subdomain", "properties": {"value": "api.example.com"}},
    ]
    edge = {"type": "has_subdomain", "source_ref": {"node_index": 0}, "target_ref": {"node_index": 1}}
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        created = await kb.write(write({"nodes": nodes, "relations": [{**edge, "properties": {"seen_by": "dnsx"}}]}))
        await kb.write(write({"relations": [{"id": created["relations"][0]["id"], "properties": {}}]}))
        again = await kb.write(write({"nodes": nodes, "relations": [{**edge, "properties": {"round": 2}}]}))

        assert again["relations"][0]["id"] == created["relations"][0]["id"]
        assert seen == [{"seen_by": "dnsx"}, {"seen_by": "dnsx"}, {"seen_by": "dnsx", "round": 2}]


@pytest.mark.parametrize(
    ("nodes", "relation", "message"),
    [
        (
            [
                {"type": "ip_address", "properties": {"value": "192.0.2.1", "version": 4}},
                {"type": "subdomain", "properties": {"value": "api.other.com"}},
            ],
            {"type": "has_subdomain", "target_ref": {"node_index": 1}, "properties": {}},
            "relation endpoint types are not allowed",
        ),
        (
            [{"type": "ip_cidr", "properties": {"value": "192.0.2.0/24", "version": 4}}],
            {"type": "contains_cidr", "target_ref": {"node_index": 0}, "properties": {}},
            "self edge is not allowed",
        ),
        (
            [{"type": "advisory", "properties": {"value": "CVE-2025-29927"}}],
            {"type": "aliases", "target_ref": {"node_index": 0}, "properties": {}},
            "self edge is not allowed",
        ),
        (
            [
                {"type": "domain", "properties": {"value": "example.com"}},
                {"type": "endpoint", "properties": {"url": "https://iodef.example.com/r", "method": "GET"}},
            ],
            {"type": "has_contact", "target_ref": {"node_index": 1}, "properties": {"role": "Abuse"}},
            "/properties/role: expected",
        ),
    ],
)
async def test_endpoint_value_errors_come_after_the_type_and_property_gates(tmp_path, nodes, relation, message):
    """Moving the value check after the merge fixes which error a doubly invalid
    write reports. The endpoint type and self-edge gate still come first, then the relation's own
    properties, and only then the endpoint values; each case below also fails its value check."""
    request = {"nodes": nodes, "relations": [{**relation, "source_ref": {"node_index": 0}}]}
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        with pytest.raises(InvalidParamsError, match=message):
            await kb.write(write(request))


async def test_a_registration_is_scoped_to_one_domain_and_keyed_by_its_registry_id(tmp_path):
    """A registration needs its `has_registration` edge in the same write, cannot move to a
    second domain, and a re-registration under a new registry id is a second node."""
    registration = {"registry": "com", "registry_domain_id": "2336799_DOMAIN_COM-VRSN"}
    domain = {"type": "domain", "properties": {"value": "example.com"}}
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        with pytest.raises(InvalidParamsError, match="exactly one has_registration"):
            await kb.write(write({"nodes": [domain, {"type": "whois_registration", "properties": registration}]}))

        edge = {"type": "has_registration", "source_ref": {"node_index": 0}, "target_ref": {"node_index": 1}}
        created = await kb.write(
            write(
                {
                    "nodes": [domain, {"type": "whois_registration", "properties": registration}],
                    "relations": [{**edge, "properties": {}}],
                }
            )
        )
        other = await kb.write(write({"nodes": [{"type": "domain", "properties": {"value": "example.net"}}]}))
        with pytest.raises(InvalidParamsError, match="different parent"):
            await kb.write(
                write(
                    {
                        "relations": [
                            {
                                "type": "has_registration",
                                "source_ref": {"id": other["nodes"][0]["id"]},
                                "target_ref": {"id": created["nodes"][1]["id"]},
                                "properties": {},
                            }
                        ]
                    }
                )
            )
        with pytest.raises(InvalidParamsError, match="relation endpoint constraint failed"):
            await kb.write(
                write(
                    {
                        "nodes": [
                            {"type": "domain", "properties": {"value": "example.org"}},
                            {"type": "whois_registration", "properties": registration},
                        ],
                        "relations": [{**edge, "properties": {}}],
                    }
                )
            )
        renewed = {**registration, "registration_expires": "2027-08-13T04:00:00Z"}
        again = await kb.write(
            write(
                {
                    "nodes": [domain, {"type": "whois_registration", "properties": renewed}],
                    "relations": [{**edge, "properties": {}}],
                }
            )
        )
        dropped = {"registry": "com", "registry_domain_id": "9999999_DOMAIN_COM-VRSN"}
        reregistered = await kb.write(
            write(
                {
                    "nodes": [domain, {"type": "whois_registration", "properties": dropped}],
                    "relations": [{**edge, "properties": {}}],
                }
            )
        )

        assert again["nodes"][1]["id"] == created["nodes"][1]["id"]
        assert reregistered["nodes"][1]["id"] != created["nodes"][1]["id"]


async def test_a_contact_role_is_checked_on_the_merged_edge(tmp_path):
    """An id patch that omits `role` still passes the role gate, because the check reads the
    stored role through the merge; a registration role on the name itself is refused."""
    registration = {"registry": "com", "registry_domain_id": "2336799_DOMAIN_COM-VRSN"}
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        created = await kb.write(
            write(
                {
                    "nodes": [
                        {"type": "domain", "properties": {"value": "example.com"}},
                        {"type": "whois_registration", "properties": registration},
                        {"type": "email_address", "properties": {"value": "hostmaster@example.com"}},
                    ],
                    "relations": [
                        {
                            "type": "has_registration",
                            "source_ref": {"node_index": 0},
                            "target_ref": {"node_index": 1},
                            "properties": {},
                        },
                        {
                            "type": "has_contact",
                            "source_ref": {"node_index": 1},
                            "target_ref": {"node_index": 2},
                            "properties": {"role": "registrant"},
                        },
                    ],
                }
            )
        )
        contact = created["relations"][1]["id"]
        patched = await kb.write(write({"relations": [{"id": contact, "properties": {"seen_by": "rdap"}}]}))
        assert patched["relations"][0]["updated"] is True

        iodef = await kb.write(
            write(
                {
                    "nodes": [
                        {"type": "endpoint", "properties": {"url": "https://iodef.example.com/r", "method": "POST"}}
                    ],
                    "relations": [
                        {
                            "type": "has_contact",
                            "source_ref": {"id": created["nodes"][0]["id"]},
                            "target_ref": {"node_index": 0},
                            "properties": {"role": "iodef"},
                        }
                    ],
                }
            )
        )
        await kb.write(write({"relations": [{"id": iodef["relations"][0]["id"], "properties": {"seen_by": "dnsx"}}]}))

        with pytest.raises(InvalidParamsError, match="attach to a whois_registration"):
            await kb.write(
                write(
                    {
                        "relations": [
                            {
                                "type": "has_contact",
                                "source_ref": {"id": created["nodes"][0]["id"]},
                                "target_ref": {"id": created["nodes"][2]["id"]},
                                "properties": {"role": "registrant"},
                            }
                        ]
                    }
                )
            )


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


async def test_a_finding_scopes_to_a_parameter_that_is_itself_scoped_to_an_endpoint(tmp_path):
    """The round-4 has_finding widening makes a scoped node a scope-relation source for the first
    time, so the write has to resolve parameter identity before it can key the finding on it."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        request = {
            "nodes": [
                {"type": "endpoint", "properties": {"url": "https://example.com/a", "method": "GET"}},
                {"type": "endpoint", "properties": {"url": "https://example.com/b", "method": "GET"}},
                {"type": "parameter", "properties": {"name": "id", "location": "query"}},
                {"type": "parameter", "properties": {"name": "id", "location": "query"}},
                {
                    "type": "finding",
                    "properties": {
                        "rule": "manual:reflected-value",
                        "matcher": "",
                        "title": "reflected value",
                        "severity": "medium",
                    },
                },
                {
                    "type": "finding",
                    "properties": {
                        "rule": "manual:reflected-value",
                        "matcher": "",
                        "title": "reflected value",
                        "severity": "medium",
                    },
                },
            ],
            "relations": [
                {
                    "type": "has_parameter",
                    "source_ref": {"node_index": 0},
                    "target_ref": {"node_index": 2},
                    "properties": {},
                },
                {
                    "type": "has_parameter",
                    "source_ref": {"node_index": 1},
                    "target_ref": {"node_index": 3},
                    "properties": {},
                },
                {
                    "type": "has_finding",
                    "source_ref": {"node_index": 2},
                    "target_ref": {"node_index": 4},
                    "properties": {},
                },
                {
                    "type": "has_finding",
                    "source_ref": {"node_index": 3},
                    "target_ref": {"node_index": 5},
                    "properties": {},
                },
            ],
        }
        created = await kb.write(write(request))
        identifiers = [node["id"] for node in created["nodes"]]

        assert identifiers[2] != identifiers[3]
        assert identifiers[4] != identifiers[5]
        assert await kb.workers.read(lambda c, t: c.execute("select count(*) from nodes").get) == 6


async def test_round_four_pivot_nodes_write_and_reject_their_cross_field_mismatches(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        created = await kb.write(
            write(
                {
                    "nodes": [
                        {"type": "endpoint", "properties": {"url": "https://example.com/", "method": "GET"}},
                        {"type": "http_fingerprint", "properties": {"kind": "favicon_mmh3", "value": "-1752256170"}},
                        {"type": "storage_bucket", "properties": {"provider": "aws_s3", "name": "example-assets"}},
                    ],
                    "relations": [
                        {
                            "type": "has_http_fingerprint",
                            "source_ref": {"node_index": 0},
                            "target_ref": {"node_index": 1},
                            "properties": {},
                        },
                        {
                            "type": "backed_by_bucket",
                            "source_ref": {"node_index": 0},
                            "target_ref": {"node_index": 2},
                            "properties": {},
                        },
                    ],
                }
            )
        )

        assert [node["created"] for node in created["nodes"]] == [True, True, True]
        with pytest.raises(InvalidParamsError, match="favicon_mmh3"):
            await kb.write(
                write(
                    {
                        "nodes": [
                            {"type": "http_fingerprint", "properties": {"kind": "favicon_mmh3", "value": "a" * 64}}
                        ],
                        "relations": [],
                    }
                )
            )
        with pytest.raises(InvalidParamsError, match="azure_blob bucket spelling"):
            await kb.write(
                write(
                    {
                        "nodes": [
                            {"type": "storage_bucket", "properties": {"provider": "azure_blob", "name": "a-b-c"}}
                        ],
                        "relations": [],
                    }
                )
            )


async def test_a_host_key_is_shared_by_every_service_that_presents_it(tmp_path):
    """The pivot is the point: one cloned image serving one key must be one node, not one per host."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        nodes: list[dict[str, object]] = []
        relations: list[dict[str, object]] = []
        for index, address in enumerate(("192.0.2.10", "192.0.2.11")):
            base = index * 3
            nodes.extend(
                [
                    {"type": "ip_address", "properties": {"value": address, "version": 4}},
                    {"type": "port", "properties": {"transport": "tcp", "number": 22}},
                    {"type": "service", "properties": {"name": "ssh"}},
                ]
            )
            relations.extend(
                [
                    {
                        "type": "has_open_port",
                        "source_ref": {"node_index": base},
                        "target_ref": {"node_index": base + 1},
                        "properties": {},
                    },
                    {
                        "type": "has_service",
                        "source_ref": {"node_index": base + 1},
                        "target_ref": {"node_index": base + 2},
                        "properties": {},
                    },
                    {
                        "type": "presents_host_key",
                        "source_ref": {"node_index": base + 2},
                        "target_ref": {"node_index": 6},
                        "properties": {},
                    },
                ]
            )
        nodes.append({"type": "host_key", "properties": {"algorithm": "ssh-ed25519", "fingerprint_sha256": "b" * 64}})

        created = await kb.write(write({"nodes": nodes, "relations": relations}))

        assert [node["created"] for node in created["nodes"]] == [True] * 7
        assert (
            await kb.workers.read(lambda c, t: c.execute("select count(*) from nodes where type='host_key'").get) == 1
        )


async def test_the_widened_endpoint_whitelists_accept_the_writes_they_were_widened_for(tmp_path):
    """dnsx and cdncheck name a CDN from DNS alone, before a port is probed, and a CVE template
    matches at a URL rather than at a negotiated service."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        written = await kb.write(
            write(
                {
                    "nodes": [
                        {"type": "domain", "properties": {"value": "example.com"}},
                        {"type": "subdomain", "properties": {"value": "www.example.com"}},
                        {"type": "technology", "properties": {"name": "cloudflare"}},
                        {"type": "endpoint", "properties": {"url": "https://www.example.com/", "method": "GET"}},
                        {"type": "advisory", "properties": {"value": "CVE-2026-1234"}},
                    ],
                    "relations": [
                        {
                            "type": "protected_by",
                            "source_ref": {"node_index": 1},
                            "target_ref": {"node_index": 2},
                            "properties": {"kind": "cdn"},
                        },
                        {
                            "type": "runs_technology",
                            "source_ref": {"node_index": 0},
                            "target_ref": {"node_index": 2},
                            "properties": {},
                        },
                        {
                            "type": "affected_by",
                            "source_ref": {"node_index": 3},
                            "target_ref": {"node_index": 4},
                            "properties": {},
                        },
                    ],
                }
            )
        )

        assert [relation["created"] for relation in written["relations"]] == [True, True, True]
        with pytest.raises(InvalidParamsError, match="endpoint types are not allowed"):
            await kb.write(
                write(
                    {
                        "nodes": [{"type": "asn", "properties": {"value": 64512}}],
                        "relations": [
                            {
                                "type": "runs_technology",
                                "source_ref": {"node_index": 0},
                                "target_ref": {"id": written["nodes"][2]["id"]},
                                "properties": {},
                            }
                        ],
                    }
                )
            )


async def test_epss_and_kev_on_a_non_cve_advisory_are_refused_after_a_patch_too(tmp_path):
    """R3, KTD2: only CVEs are scored by EPSS and listed in KEV. The check reruns on the merged
    stored properties, so a GHSA written bare and later patched by ID with an EPSS score is refused."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        written = await kb.write(
            write(
                {
                    "nodes": [
                        {"type": "endpoint", "properties": {"url": "https://www.example.com/", "method": "GET"}},
                        {"type": "advisory", "properties": {"value": "GHSA-f82v-jwr5-mffw", "cvss_score": 9.1}},
                        {"type": "advisory", "properties": {"value": "CVE-2025-29927", "epss_score": 0.92}},
                        {"type": "cwe", "properties": {"value": "CWE-285"}},
                    ],
                    "relations": [
                        {
                            "type": "affected_by",
                            "source_ref": {"node_index": 0},
                            "target_ref": {"node_index": 1},
                            "properties": {},
                        },
                        {
                            "type": "has_weakness",
                            "source_ref": {"node_index": 1},
                            "target_ref": {"node_index": 3},
                            "properties": {},
                        },
                    ],
                }
            )
        )
        ghsa = written["nodes"][1]["id"]

        with pytest.raises(InvalidParamsError, match="epss_score"):
            await kb.write(write({"nodes": [{"id": ghsa, "properties": {"epss_score": 0.92}}]}))
        with pytest.raises(InvalidParamsError, match="kev_added"):
            await kb.write(write({"nodes": [{"id": ghsa, "properties": {"kev_added": "2025-03-24"}}]}))
        assert "epss_score" not in (await record_of(kb, "nodes", ghsa))["properties"]


async def test_a_cve_learned_later_reaches_the_affected_object_in_one_alias_hop(tmp_path):
    """AE3, R5: an object written affected by a GHSA stays reachable from the CVE learned a week
    later. The alias edge points toward the CVE, so the reverse edge is refused."""
    endpoint = {"type": "endpoint", "properties": {"url": "https://app.acme.com/", "method": "GET"}}
    ghsa_node = {"type": "advisory", "properties": {"value": "GHSA-f82v-jwr5-mffw"}}
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        first = await kb.write(
            write(
                {
                    "nodes": [endpoint, ghsa_node],
                    "relations": [
                        {
                            "type": "affected_by",
                            "source_ref": {"node_index": 0},
                            "target_ref": {"node_index": 1},
                            "properties": {},
                        }
                    ],
                }
            )
        )
        app, ghsa = (node["id"] for node in first["nodes"])
        cve_node = {"type": "advisory", "properties": {"value": "CVE-2025-29927"}}
        with pytest.raises(InvalidParamsError, match="an alias points toward the CVE"):
            await kb.write(
                write(
                    {
                        "nodes": [cve_node],
                        "relations": [
                            {
                                "type": "aliases",
                                "source_ref": {"node_index": 0},
                                "target_ref": {"id": ghsa},
                                "properties": {},
                            }
                        ],
                    }
                )
            )
        later = await kb.write(
            write(
                {
                    "nodes": [cve_node],
                    "relations": [
                        {
                            "type": "aliases",
                            "source_ref": {"id": ghsa},
                            "target_ref": {"node_index": 0},
                            "properties": {},
                        }
                    ],
                }
            )
        )
        cve = later["nodes"][0]["id"]

        async def sources_of(relation: str, target: str) -> list[str]:
            found = await kb.search(SearchRequest(kind="relations", type=relation, target_id=target))
            return [(await record_of(kb, "relations", item["id"]))["source_id"] for item in found["items"]]

        assert await sources_of("aliases", cve) == [ghsa]
        assert await sources_of("affected_by", ghsa) == [app]


# Inventory state. These writes build the request directly: `write()` would state `candidate` for
# every creating carrying node, which hides the missing-ownership refusals under test.
ACME = {"type": "domain", "properties": {"value": "acme.com"}}
HOST = {"type": "ip_address", "properties": {"value": "192.0.2.10", "version": 4}}


def port(number: int) -> dict[str, object]:
    return {"type": "port", "properties": {"transport": "tcp", "number": number}}


def open_port(parent: dict[str, object], child: int) -> dict[str, object]:
    return {"type": "has_open_port", "source_ref": parent, "target_ref": {"node_index": child}, "properties": {}}


async def stored_state(kb, identifier):
    return await kb.workers.read(
        lambda c, t: c.execute(
            "select ownership,authorization,allowlist_scoped,authorization_override,state_root_uuid from nodes where uuid=?",
            (identifier,),
        ).fetchone()
    )


async def effective_authorizations(kb, *identifiers):
    def read(connection, token):
        rows = [row_by_id(connection, "nodes", identifier) or {} for identifier in identifiers]
        states = [effective_state(connection, row) for row in rows]
        return [state.authorization if state is not None else None for state in states]

    return await kb.workers.read(read)


async def node_count(kb):
    return await kb.workers.read(lambda c, t: c.execute("select count(*) from nodes").get)


async def classified_host(kb, evidence, *ports):
    """Write an owned, in_scope address with the given ports under it; return the address and port IDs."""
    host = {**HOST, "ownership": "owned", "authorization": "in_scope", "evidence_add": [evidence]}
    result = await kb.write(
        WriteRequest.model_validate(
            {
                "nodes": [host, *(port(number) for number in ports)],
                "relations": [open_port({"node_index": 0}, index + 1) for index in range(len(ports))],
            }
        )
    )
    return [node["id"] for node in result["nodes"]]


@pytest.mark.parametrize(
    ("node", "message"),
    [
        ({"type": "subdomain", "properties": {"value": "api.acme.com"}}, r"^nodes\[1\]: ownership is required"),
        ({**ACME, "ownership": "rejected"}, r"^nodes\[1\]: .*cannot be created as rejected"),
        ({**ACME, "ownership": "owned"}, r"^nodes\[1\]: .*requires evidence_add"),
        ({**ACME, "ownership": "candidate", "authorization": "in_scope"}, r"^nodes\[1\]: .*requires evidence_add"),
        (
            {"type": "advisory", "properties": {"value": "CVE-2026-1234"}, "ownership": "owned"},
            r"^nodes\[1\]: advisory carries no inventory state",
        ),
    ],
)
async def test_a_creation_without_a_valid_state_claim_is_refused_with_zero_rows(tmp_path, node, message):
    """AE1, R1, R5, R6: the state pass refuses the whole batch before any row is written."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        with pytest.raises(InvalidParamsError, match=message):
            await kb.write(WriteRequest.model_validate({"nodes": [{**HOST, "ownership": "candidate"}, node]}))
        assert await node_count(kb) == 0


async def test_a_creation_defaults_authorization_to_unknown_and_echoes_it(tmp_path):
    """R3: a candidate creation needs no evidence and reads back as candidate and unknown."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        result = await kb.write(WriteRequest.model_validate({"nodes": [{**ACME, "ownership": "candidate"}]}))
        node = result["nodes"][0]
        assert (node["ownership"], node["authorization"]) == ("candidate", "unknown")
        assert await stored_state(kb, node["id"]) == ("candidate", "unknown", 0, None, None)


async def test_promotion_by_id_needs_evidence_named_in_the_same_write(tmp_path):
    """AE2, R6, KTD9: already-linked evidence counts once it is named again in `evidence_add`."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = (await evidence_fixture(kb, 1))[0]
        created = await kb.write(
            WriteRequest.model_validate({"nodes": [{**ACME, "ownership": "candidate", "evidence_add": [evidence]}]})
        )
        identifier = created["nodes"][0]["id"]
        with pytest.raises(InvalidParamsError, match=r"^nodes\[0\]: moving ownership from candidate to owned"):
            await kb.write(WriteRequest.model_validate({"nodes": [{"id": identifier, "ownership": "owned"}]}))
        assert (await stored_state(kb, identifier))[0] == "candidate"
        promoted = await kb.write(
            WriteRequest.model_validate(
                {"nodes": [{"id": identifier, "ownership": "owned", "evidence_add": [evidence]}]}
            )
        )
        assert promoted["nodes"][0]["ownership"] == "owned"
        assert promoted["nodes"][0]["links_added"] == 0


async def test_pending_index_evidence_counts_and_deleting_evidence_is_refused(tmp_path):
    """KTD9: evidence whose index is still pending counts; delete_pending evidence stays RECORD_DELETING."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        pending, deleting = await evidence_fixture(kb, 2)
        assert (
            await kb.workers.read(
                lambda c, t: c.execute("select index_state from evidence where uuid=?", (pending,)).get
            )
            == "pending"
        )
        created = await kb.write(
            WriteRequest.model_validate(
                {"nodes": [{**ACME, "ownership": "candidate"}, {**HOST, "ownership": "candidate"}]}
            )
        )
        acme, host = [node["id"] for node in created["nodes"]]
        promoted = await kb.write(
            WriteRequest.model_validate({"nodes": [{"id": acme, "ownership": "dependency", "evidence_add": [pending]}]})
        )
        assert promoted["nodes"][0]["ownership"] == "dependency"
        await admit(kb, "evidence", [deleting])
        with pytest.raises(RecordConflictError, match=r"^RECORD_DELETING$"):
            await kb.write(
                WriteRequest.model_validate({"nodes": [{"id": host, "ownership": "owned", "evidence_add": [deleting]}]})
            )


async def test_an_identity_rescan_never_changes_a_stored_classification(tmp_path):
    """AE8, R19: carried state applies only on creation, and the acknowledgement echoes stored state."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = (await evidence_fixture(kb, 1))[0]
        created = await kb.write(
            WriteRequest.model_validate(
                {"nodes": [{**ACME, "ownership": "owned", "authorization": "in_scope", "evidence_add": [evidence]}]}
            )
        )
        identifier = created["nodes"][0]["id"]
        rescan = await kb.write(WriteRequest.model_validate({"nodes": [{**ACME, "ownership": "candidate"}]}))
        node = rescan["nodes"][0]
        assert (node["id"], node["ownership"], node["authorization"]) == (identifier, "owned", "in_scope")
        await kb.write(
            WriteRequest.model_validate({"nodes": [{**ACME, "ownership": "rejected", "evidence_add": [evidence]}]})
        )
        await kb.write(WriteRequest.model_validate({"nodes": [ACME]}))
        assert await stored_state(kb, identifier) == ("owned", "in_scope", 0, None, None)


@pytest.mark.parametrize("authorization", ["in_scope", "unknown"])
async def test_an_identity_rescan_never_changes_a_scoped_childs_override(tmp_path, authorization):
    """R19: under an allowlist root, a rescan could otherwise widen a narrowed port or clear its override."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = (await evidence_fixture(kb, 1))[0]
        host, ssh = await classified_host(kb, evidence, 22)
        await kb.write(
            WriteRequest.model_validate(
                {
                    "nodes": [
                        {"id": host, "allowlist_scoped": True, "evidence_add": [evidence]},
                        {"id": ssh, "authorization": "out_of_scope", "evidence_add": [evidence]},
                    ]
                }
            )
        )
        rescan = await kb.write(
            WriteRequest.model_validate(
                {
                    "nodes": [HOST, {**port(22), "authorization": authorization, "evidence_add": [evidence]}],
                    "relations": [open_port({"node_index": 0}, 1)],
                }
            )
        )
        node = rescan["nodes"][1]
        assert (node["id"], node["authorization"]) == (ssh, "out_of_scope")
        assert (await stored_state(kb, ssh))[3] == "out_of_scope"


async def test_the_refused_transition_cells_name_the_current_state(tmp_path):
    """R20: owned cannot be rejected, rejected cannot return to candidate, and a rejected node's
    authorization is frozen, while a rejected node may still be attributed by ID with evidence."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = (await evidence_fixture(kb, 1))[0]
        created = await kb.write(
            WriteRequest.model_validate(
                {
                    "nodes": [
                        {**ACME, "ownership": "owned", "evidence_add": [evidence]},
                        {**HOST, "ownership": "candidate"},
                    ]
                }
            )
        )
        acme, host = [node["id"] for node in created["nodes"]]
        with pytest.raises(ConflictError, match=r"^nodes\[0\]: .*owned .*to rejected.*withdraw to candidate first"):
            await kb.write(
                WriteRequest.model_validate(
                    {"nodes": [{"id": acme, "ownership": "rejected", "evidence_add": [evidence]}]}
                )
            )
        rejected = await kb.write(
            WriteRequest.model_validate({"nodes": [{"id": host, "ownership": "rejected", "evidence_add": [evidence]}]})
        )
        assert (rejected["nodes"][0]["ownership"], rejected["nodes"][0]["authorization"]) == ("rejected", "unknown")
        with pytest.raises(ConflictError, match=r"^nodes\[0\]: .*from rejected to candidate"):
            await kb.write(WriteRequest.model_validate({"nodes": [{"id": host, "ownership": "candidate"}]}))
        with pytest.raises(ConflictError, match=r"^nodes\[0\]: authorization cannot change on a rejected"):
            await kb.write(
                WriteRequest.model_validate(
                    {"nodes": [{"id": host, "authorization": "out_of_scope", "evidence_add": [evidence]}]}
                )
            )
        restored = await kb.write(
            WriteRequest.model_validate(
                {"nodes": [{"id": host, "ownership": "dependency", "evidence_add": [evidence]}]}
            )
        )
        assert restored["nodes"][0]["ownership"] == "dependency"
        withdrawn = await kb.write(WriteRequest.model_validate({"nodes": [{"id": acme, "ownership": "candidate"}]}))
        assert withdrawn["nodes"][0]["ownership"] == "candidate"


async def test_a_scoped_child_inherits_its_roots_state_and_cannot_state_ownership(tmp_path):
    """AE5, R5, KTD4: the state root is the carrying root's planned UUID, even two levels down."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = (await evidence_fixture(kb, 1))[0]
        request = scoped_stack(("service", "port", "ip_address"))
        request["nodes"][2].update(ownership="owned", authorization="in_scope", evidence_add=[evidence])
        result = await kb.write(WriteRequest.model_validate(request))
        service, child, host = result["nodes"]
        assert [(node["ownership"], node["authorization"]) for node in result["nodes"]] == [("owned", "in_scope")] * 3
        assert (await stored_state(kb, child["id"])) == (None, None, 0, None, host["id"])
        assert (await stored_state(kb, service["id"])) == (None, None, 0, None, host["id"])
        with pytest.raises(InvalidParamsError, match=r"^nodes\[1\]: a port inherits its ownership"):
            await kb.write(
                WriteRequest.model_validate(
                    {
                        "nodes": [HOST, {**port(22), "ownership": "owned"}],
                        "relations": [open_port({"node_index": 0}, 1)],
                    }
                )
            )
        with pytest.raises(InvalidParamsError, match=r"^nodes\[0\]: a port inherits its ownership"):
            await kb.write(WriteRequest.model_validate({"nodes": [{"id": child["id"], "allowlist_scoped": True}]}))


async def test_a_scoped_child_narrows_to_out_of_scope_with_evidence(tmp_path):
    """AE7, R16: the narrowing reaches the service under the port, and in_scope needs an allowlist root."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = (await evidence_fixture(kb, 1))[0]
        host, ssh, https = await classified_host(kb, evidence, 22, 443)
        service = (
            await kb.write(
                WriteRequest.model_validate(
                    {
                        "nodes": [{"type": "service", "properties": {"name": "ssh"}}],
                        "relations": [
                            {
                                "type": "has_service",
                                "source_ref": {"id": ssh},
                                "target_ref": {"node_index": 0},
                                "properties": {},
                            }
                        ],
                    }
                )
            )
        )["nodes"][0]
        assert service["authorization"] == "in_scope"
        with pytest.raises(InvalidParamsError, match=r"^nodes\[0\]: .*out_of_scope requires evidence_add"):
            await kb.write(WriteRequest.model_validate({"nodes": [{"id": ssh, "authorization": "out_of_scope"}]}))
        narrowed = await kb.write(
            WriteRequest.model_validate(
                {"nodes": [{"id": ssh, "authorization": "out_of_scope", "evidence_add": [evidence]}]}
            )
        )
        assert (narrowed["nodes"][0]["ownership"], narrowed["nodes"][0]["authorization"]) == ("owned", "out_of_scope")
        effective = await effective_authorizations(kb, host, ssh, service["id"], https)
        assert effective == ["in_scope", "out_of_scope", "out_of_scope", "in_scope"]
        with pytest.raises(ConflictError, match=r"^nodes\[0\]: an in_scope override needs an allowlist-scoped root"):
            await kb.write(
                WriteRequest.model_validate(
                    {"nodes": [{"id": https, "authorization": "in_scope", "evidence_add": [evidence]}]}
                )
            )
        cleared = await kb.write(WriteRequest.model_validate({"nodes": [{"id": ssh, "authorization": "unknown"}]}))
        assert cleared["nodes"][0]["authorization"] == "in_scope"
        assert (await stored_state(kb, ssh))[3] is None


async def test_the_allowlist_marker_needs_in_scope_and_evidence_and_gates_its_children(tmp_path):
    """R27, AE11: an allowlist root leaves unlisted children out_of_scope until an ID write widens them."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = (await evidence_fixture(kb, 1))[0]
        host, https, alternate = await classified_host(kb, evidence, 443, 8080)
        candidate = (await kb.write(WriteRequest.model_validate({"nodes": [{**ACME, "ownership": "candidate"}]})))[
            "nodes"
        ][0]["id"]
        with pytest.raises(InvalidParamsError, match=r"^nodes\[0\]: .*allowlist_scoped requires evidence_add"):
            await kb.write(WriteRequest.model_validate({"nodes": [{"id": host, "allowlist_scoped": True}]}))
        with pytest.raises(ConflictError, match=r"^nodes\[0\]: allowlist_scoped requires authorization in_scope"):
            await kb.write(
                WriteRequest.model_validate(
                    {"nodes": [{"id": candidate, "allowlist_scoped": True, "evidence_add": [evidence]}]}
                )
            )
        marked = await kb.write(
            WriteRequest.model_validate({"nodes": [{"id": host, "allowlist_scoped": True, "evidence_add": [evidence]}]})
        )
        assert marked["nodes"][0]["authorization"] == "in_scope"
        widened = await kb.write(
            WriteRequest.model_validate(
                {"nodes": [{"id": https, "authorization": "in_scope", "evidence_add": [evidence]}]}
            )
        )
        assert widened["nodes"][0]["authorization"] == "in_scope"
        effective = await effective_authorizations(kb, host, https, alternate)
        assert effective == ["in_scope", "in_scope", "out_of_scope"]
        with pytest.raises(ConflictError, match=r"^nodes\[0\]: allowlist_scoped requires authorization in_scope"):
            await kb.write(WriteRequest.model_validate({"nodes": [{"id": host, "authorization": "unknown"}]}))


async def test_an_allowlist_root_in_the_same_batch_admits_an_earlier_in_scope_child(tmp_path):
    """KTD3: the state pass reads the root's planned state, whatever the request order."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = (await evidence_fixture(kb, 1))[0]
        result = await kb.write(
            WriteRequest.model_validate(
                {
                    "nodes": [
                        {**port(443), "authorization": "in_scope", "evidence_add": [evidence]},
                        port(8080),
                        {
                            **HOST,
                            "ownership": "owned",
                            "authorization": "in_scope",
                            "allowlist_scoped": True,
                            "evidence_add": [evidence],
                        },
                    ],
                    "relations": [open_port({"node_index": 2}, 0), open_port({"node_index": 2}, 1)],
                }
            )
        )
        assert [node["authorization"] for node in result["nodes"]] == ["in_scope", "out_of_scope", "in_scope"]


async def test_removing_the_last_ready_evidence_of_a_claim_is_refused(tmp_path):
    """R21, KTD9: a delete_pending evidence item no longer counts as a remaining link."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        first, second, third = await evidence_fixture(kb, 3)
        created = await kb.write(
            WriteRequest.model_validate(
                {
                    "nodes": [
                        {**ACME, "ownership": "owned", "evidence_add": [first, second]},
                        {**HOST, "ownership": "owned", "evidence_add": [first, third]},
                    ]
                }
            )
        )
        acme, host = [node["id"] for node in created["nodes"]]
        removed = await kb.write(WriteRequest.model_validate({"nodes": [{"id": acme, "evidence_remove": [first]}]}))
        assert removed["nodes"][0]["links_removed"] == 1
        with pytest.raises(ConflictError, match=r"^nodes\[0\]: .*last ready evidence"):
            await kb.write(WriteRequest.model_validate({"nodes": [{"id": acme, "evidence_remove": [second]}]}))
        await admit(kb, "evidence", [third])
        with pytest.raises(ConflictError, match=r"^nodes\[0\]: .*last ready evidence"):
            await kb.write(WriteRequest.model_validate({"nodes": [{"id": host, "evidence_remove": [first]}]}))
        assert await kb.workers.read(lambda c, t: c.execute("select count(*) from node_evidence").get) == 3


async def test_a_rejection_and_a_narrowed_child_keep_their_last_evidence_and_a_swap_is_allowed(tmp_path):
    """R21: `rejected` and an authorization override are claims too; adding new evidence while
    removing the old leaves a link, so the claim stays evidenced."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        first, second, third = await evidence_fixture(kb, 3)
        (staging,) = await candidates(kb, STAGING)
        await reject(kb, staging, first)
        with pytest.raises(ConflictError, match=r"^nodes\[0\]: the domain holds a rejected claim"):
            await kb.write(WriteRequest.model_validate({"nodes": [{"id": staging, "evidence_remove": [first]}]}))
        _host, ssh = await classified_host(kb, first, 22)
        await kb.write(
            WriteRequest.model_validate(
                {"nodes": [{"id": ssh, "authorization": "out_of_scope", "evidence_add": [second]}]}
            )
        )
        with pytest.raises(ConflictError, match=r"^nodes\[0\]: the port holds a out_of_scope claim"):
            await kb.write(WriteRequest.model_validate({"nodes": [{"id": ssh, "evidence_remove": [second]}]}))
        swapped = await kb.write(
            WriteRequest.model_validate({"nodes": [{"id": ssh, "evidence_add": [third], "evidence_remove": [second]}]})
        )
        assert (swapped["nodes"][0]["links_added"], swapped["nodes"][0]["links_removed"]) == (1, 1)


# First and last seen. Each time is written as `observed_at` and read back in the canonical form.
T1, T2 = "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"
T1_SEEN, T2_SEEN = "2026-01-01T00:00:00.000000Z", "2026-02-01T00:00:00.000000Z"


async def record_of(kb, kind, identifier):
    return (await kb.get(GetRequest(kind=kind, ids=[identifier])))["records"][0]


async def seen(kb, kind, identifier):
    record = await record_of(kb, kind, identifier)
    assert "observed_at" not in record
    return record["first_seen"], record["last_seen"]


async def test_an_older_observation_lowers_first_seen_and_a_state_change_is_no_observation(tmp_path):
    """AE6, R12, R13: an older rescan moves first seen back, and promotion, label, source and evidence do not."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = (await evidence_fixture(kb, 1))[0]
        created = await kb.write(
            WriteRequest.model_validate({"nodes": [{**ACME, "ownership": "candidate", "observed_at": T2}]})
        )
        identifier = created["nodes"][0]["id"]
        assert await seen(kb, "nodes", identifier) == (T2_SEEN, T2_SEEN)
        await kb.write(WriteRequest.model_validate({"nodes": [{**ACME, "ownership": "candidate", "observed_at": T1}]}))
        assert await seen(kb, "nodes", identifier) == (T1_SEEN, T2_SEEN)
        await kb.write(
            WriteRequest.model_validate(
                {
                    "nodes": [
                        {
                            "id": identifier,
                            "ownership": "owned",
                            "label": "Acme",
                            "source": "registrar",
                            "evidence_add": [evidence],
                        }
                    ]
                }
            )
        )
        assert await seen(kb, "nodes", identifier) == (T1_SEEN, T2_SEEN)


async def test_an_older_observation_only_adds_the_properties_a_record_lacks(tmp_path):
    """AE6, R28, KTD6: the stored 1.25 survives a T1 scan of 1.18, which adds `product` and removes nothing."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        stack = scoped_stack()
        stack["nodes"][2] = {
            "type": "service",
            "properties": {"name": "http", "version": "1.25", "secure": False, "banner": "welcome"},
            "observed_at": T2,
        }
        service = (await kb.write(WriteRequest.model_validate(stack)))["nodes"][2]["id"]
        await kb.write(
            WriteRequest.model_validate(
                {
                    "nodes": [
                        {
                            "id": service,
                            "properties": {"version": "1.18", "product": "nginx"},
                            "remove_properties": ["/banner"],
                            "observed_at": T1,
                        }
                    ]
                }
            )
        )
        record = await record_of(kb, "nodes", service)
        stored = {"name": "http", "version": "1.25", "secure": False, "banner": "welcome", "product": "nginx"}
        assert record["properties"] == stored
        assert (record["first_seen"], record["last_seen"]) == (T1_SEEN, T2_SEEN)


async def test_an_older_observation_adds_a_missing_key_inside_a_stored_object(tmp_path):
    """R28, KTD6: the merge descends into objects, so the stored 1.3 stays beside the older cipher."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        newer = {"value": "acme.com", "tls": {"version": "1.3"}}
        created = await kb.write(write({"nodes": [{**ACME, "properties": newer, "observed_at": T2}]}))
        identifier = created["nodes"][0]["id"]
        older = {"value": "acme.com", "tls": {"version": "1.2", "cipher": "x"}}
        await kb.write(write({"nodes": [{**ACME, "properties": older, "observed_at": T1}]}))
        record = await record_of(kb, "nodes", identifier)
        assert record["properties"] == {"value": "acme.com", "tls": {"version": "1.3", "cipher": "x"}}


async def test_an_older_relation_upsert_keeps_its_values_and_its_later_last_seen(tmp_path):
    """R12, R28: an identity re-upsert of an edge observed earlier adds a missing key and overwrites none."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:

        def resolution(properties, observed_at):
            relation = {
                "type": "resolves_to",
                "source_ref": {"node_index": 0},
                "target_ref": {"node_index": 1},
                "properties": properties,
                "observed_at": observed_at,
            }
            return write({"nodes": [ACME, HOST], "relations": [relation]})

        written = await kb.write(resolution({"vantage": "public"}, T2))
        relation = written["relations"][0]["id"]
        again = await kb.write(resolution({"vantage": "internal", "ttl": 60}, T1))
        assert again["relations"][0]["id"] == relation
        record = await record_of(kb, "relations", relation)
        assert record["properties"] == {"vantage": "public", "ttl": 60}
        assert (record["first_seen"], record["last_seen"]) == (T1_SEEN, T2_SEEN)


async def test_a_property_patch_without_observed_at_is_observed_now(tmp_path):
    """R13, KTD6: a property change reports an observation at the current time."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        created = await kb.write(
            WriteRequest.model_validate({"nodes": [{**ACME, "ownership": "candidate", "observed_at": T1}]})
        )
        identifier = created["nodes"][0]["id"]
        before = time.time_ns() // 1000
        await kb.write(WriteRequest.model_validate({"nodes": [{"id": identifier, "properties": {"note": "seen"}}]}))
        after = time.time_ns() // 1000
        first_seen, last_seen = await seen(kb, "nodes", identifier)
        assert first_seen == T1_SEEN
        assert before <= parse_timestamp(last_seen) <= after


def resolution_edge(properties: dict[str, object], **fields: object) -> dict[str, object]:
    relation = {"type": "resolves_to", "source_ref": {"node_index": 0}, "target_ref": {"node_index": 1}}
    return {**relation, "properties": properties, **fields}


async def test_an_identity_rescan_of_an_edge_without_properties_is_observed_now(tmp_path):
    """R13: a rescan is an observation even when the edge carries no properties, while an ID patch
    that changes only source or evidence is not."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = (await evidence_fixture(kb, 1))[0]
        written = await kb.write(write({"nodes": [ACME, HOST], "relations": [resolution_edge({}, observed_at=T1)]}))
        relation = written["relations"][0]["id"]
        before = time.time_ns() // 1000
        again = await kb.write(write({"nodes": [ACME, HOST], "relations": [resolution_edge({})]}))
        after = time.time_ns() // 1000
        assert again["relations"][0]["id"] == relation
        first_seen, last_seen = await seen(kb, "relations", relation)
        assert first_seen == T1_SEEN
        assert before <= parse_timestamp(last_seen) <= after
        patch = {"id": relation, "source": "dnsx", "evidence_add": [evidence]}
        await kb.write(WriteRequest.model_validate({"relations": [patch]}))
        assert await seen(kb, "relations", relation) == (first_seen, last_seen)


def moved(seconds: int) -> str:
    """The server's current time shifted by `seconds`, as an `observed_at`."""
    return format_timestamp(time.time_ns() // 1000 + seconds * 1000000)


async def test_an_observation_later_than_the_server_clock_allows_is_refused(tmp_path):
    """R12: last seen keeps the maximum, so a future `observed_at` would outrank every real scan
    until the clock caught up; one within five minutes of clock skew is accepted as given."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        future = moved(3600)
        with pytest.raises(InvalidParamsError, match=r"^nodes\[1\]: observed_at .* is later than"):
            await kb.write(write({"nodes": [ACME, {**HOST, "observed_at": future}]}))
        with pytest.raises(InvalidParamsError, match=r"^relations\[0\]: observed_at .* is later than"):
            await kb.write(write({"nodes": [ACME, HOST], "relations": [resolution_edge({}, observed_at=future)]}))
        assert await node_count(kb) == 0
        skewed = moved(60)
        created = await kb.write(write({"nodes": [{**ACME, "observed_at": skewed}]}))
        assert parse_timestamp((await seen(kb, "nodes", created["nodes"][0]["id"]))[1]) == parse_timestamp(skewed)


# Rejection (R7-R10, R17, R23, R24, R26). The job runner stays live here, so a rejection's purge
# job runs on its own; tests/storage/test_deletions.py drives it by hand where the steps matter.
STAGING = {"type": "domain", "properties": {"value": "acme-staging.net"}}


def subdomain(value: str) -> dict[str, object]:
    return {"type": "subdomain", "properties": {"value": value}}


def has_subdomain(source: int, target: int) -> dict[str, object]:
    return {
        "type": "has_subdomain",
        "source_ref": {"node_index": source},
        "target_ref": {"node_index": target},
        "properties": {},
    }


async def reject(kb, identifier, evidence):
    """Reject a node by ID and wait for its purge job; return the acknowledgement."""
    result = await kb.write(
        WriteRequest.model_validate(
            {"nodes": [{"id": identifier, "ownership": "rejected", "evidence_add": [evidence]}]}
        )
    )
    job = await kb.job_runner.wait(result["nodes"][0]["rejection_job_id"], time.monotonic() + 5)
    assert job["state"] == "completed"
    return result["nodes"][0]


async def candidates(kb, *nodes):
    result = await kb.write(
        WriteRequest.model_validate({"nodes": [{**node, "ownership": "candidate"} for node in nodes]})
    )
    return [node["id"] for node in result["nodes"]]


def rejected_items(error):
    return [(item.item, str(item.rejected_record)) for item in error.value.details.rejected_items]


async def test_rejecting_an_address_keeps_its_identity_required_properties_evidence_and_times(tmp_path):
    """R9: the stripped record keeps value and version, its evidence, source and first/last seen,
    loses its label and optional properties, resets authorization, and still validates."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence, contract = await evidence_fixture(kb, 2)
        created = await kb.write(
            WriteRequest.model_validate(
                {
                    "nodes": [
                        {
                            "type": "ip_address",
                            "properties": {"value": "192.0.2.10", "version": 4, "cloud_provider": "strangercloud"},
                            "ownership": "candidate",
                            "authorization": "out_of_scope",
                            "label": "stranger box",
                            "source": "nmap",
                            "observed_at": T1,
                            "evidence_add": [contract],
                        }
                    ]
                }
            )
        )
        identifier = created["nodes"][0]["id"]
        labelled = await kb.search(SearchRequest(kind="nodes", query="stranger box", include_evidence=False))
        assert [item["id"] for item in labelled["items"]] == [identifier]
        rejected = await reject(kb, identifier, evidence)
        assert (rejected["ownership"], rejected["authorization"]) == ("rejected", "unknown")
        record = await record_of(kb, "nodes", identifier)
        assert record["properties"] == {"value": "192.0.2.10", "version": 4}
        catalog_module.validate_record("nodes", "ip_address", record["properties"])
        assert (record["label"], record["source"], record["link_count"]) == (None, "nmap", 2)
        assert (record["first_seen"], record["last_seen"]) == (T1_SEEN, T1_SEEN)
        assert await stored_state(kb, identifier) == ("rejected", "unknown", 0, None, None)
        for query in ("stranger box", "strangercloud"):
            found = await kb.search(SearchRequest(kind="nodes", query=query, include_evidence=False))
            assert found["items"] == []


async def test_a_rejection_clears_the_allowlist_marker_and_is_no_observation(tmp_path):
    """R9, R13: an allowlisted in_scope candidate loses its marker, and the rejecting write's
    `observed_at` and properties move neither last seen nor the stored properties."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = (await evidence_fixture(kb, 1))[0]
        claims = {"authorization": "in_scope", "allowlist_scoped": True, "evidence_add": [evidence]}
        created = await kb.write(
            WriteRequest.model_validate({"nodes": [{**STAGING, "ownership": "candidate", "observed_at": T1, **claims}]})
        )
        identifier = created["nodes"][0]["id"]
        assert await stored_state(kb, identifier) == ("candidate", "in_scope", 1, None, None)
        result = await kb.write(
            WriteRequest.model_validate(
                {
                    "nodes": [
                        {
                            "id": identifier,
                            "ownership": "rejected",
                            "properties": {"note": "stranger"},
                            "observed_at": T2,
                            "evidence_add": [evidence],
                        }
                    ]
                }
            )
        )
        await kb.job_runner.wait(result["nodes"][0]["rejection_job_id"], time.monotonic() + 5)
        assert await stored_state(kb, identifier) == ("rejected", "unknown", 0, None, None)
        record = await record_of(kb, "nodes", identifier)
        assert record["properties"] == STAGING["properties"]
        assert (record["first_seen"], record["last_seen"]) == (T1_SEEN, T1_SEEN)


async def test_rejection_needs_evidence_in_the_same_write(tmp_path):
    """R8: the refusal names the item and leaves the candidate untouched."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        (identifier,) = await candidates(kb, STAGING)
        with pytest.raises(InvalidParamsError, match=r"^nodes\[0\]: moving ownership from candidate to rejected"):
            await kb.write(WriteRequest.model_validate({"nodes": [{"id": identifier, "ownership": "rejected"}]}))
        assert (await stored_state(kb, identifier))[0] == "candidate"


async def test_a_rejected_identity_and_the_subdomains_under_it_cannot_be_re_created(tmp_path):
    """AE4, AE9, R10, R17: an identity write of the rejected name or a new subdomain under it is
    refused, and the error names the rejected record by ID."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = (await evidence_fixture(kb, 1))[0]
        (staging,) = await candidates(kb, STAGING)
        await reject(kb, staging, evidence)
        before = await node_count(kb)
        with pytest.raises(RejectedIdentityError, match=r"^REJECTED_IDENTITY$") as refused:
            await kb.write(WriteRequest.model_validate({"nodes": [{**STAGING, "ownership": "candidate"}]}))
        assert rejected_items(refused) == [("nodes[0]", staging)]
        for name in ("www.acme-staging.net", "a.b.acme-staging.net"):
            with pytest.raises(RejectedIdentityError, match=r"^REJECTED_IDENTITY$") as refused:
                await kb.write(
                    WriteRequest.model_validate(
                        {"nodes": [{**ACME, "ownership": "candidate"}, {**subdomain(name), "ownership": "candidate"}]}
                    )
                )
            assert rejected_items(refused) == [("nodes[1]", staging)]
        assert await node_count(kb) == before


SDK = {"type": "package", "properties": {"purl": "pkg:npm/%40acme/sdk"}}


async def test_a_package_is_never_a_dependency_at_creation_or_by_id(tmp_path):
    """AE10, R10, KTD5: the refusal names the type and writes nothing; reclassifying a candidate
    package by ID is refused too, while every other carrying type still accepts `dependency`."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = (await evidence_fixture(kb, 1))[0]
        lodash = {"type": "package", "properties": {"purl": "pkg:npm/lodash"}}
        dependency = {"ownership": "dependency", "evidence_add": [evidence]}
        with pytest.raises(InvalidParamsError, match=r"^nodes\[0\]: a package cannot have ownership dependency"):
            await kb.write(WriteRequest.model_validate({"nodes": [{**lodash, **dependency}]}))
        assert await node_count(kb) == 0
        (sdk,) = await candidates(kb, SDK)
        with pytest.raises(InvalidParamsError, match=r"^nodes\[0\]: a package cannot have ownership dependency"):
            await kb.write(WriteRequest.model_validate({"nodes": [{"id": sdk, **dependency}]}))
        assert (await stored_state(kb, sdk))[0] == "candidate"
        written = await kb.write(WriteRequest.model_validate({"nodes": [{**ACME, **dependency}]}))
        assert written["nodes"][0]["ownership"] == "dependency"


async def test_a_rejected_package_keeps_only_its_identity_and_cannot_be_re_created(tmp_path):
    """AE6, R11: a package from an unknown publisher is a candidate; once rejected it keeps its
    purl alone, and a later identity write of the same purl is refused as a rejected identity."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = (await evidence_fixture(kb, 1))[0]
        stranger = {"type": "package", "properties": {"purl": "pkg:npm/acme-sdk", "publisher": "unknown"}}
        (identifier,) = await candidates(kb, stranger)
        assert (await reject(kb, identifier, evidence))["ownership"] == "rejected"
        assert (await record_of(kb, "nodes", identifier))["properties"] == {"purl": "pkg:npm/acme-sdk"}
        with pytest.raises(RejectedIdentityError, match=r"^REJECTED_IDENTITY$") as refused:
            await kb.write(WriteRequest.model_validate({"nodes": [{**stranger, "ownership": "candidate"}]}))
        assert rejected_items(refused) == [("nodes[0]", identifier)]


async def test_a_new_subdomain_under_a_domain_the_same_batch_rejects_is_refused(tmp_path):
    """R17, R26: the batch's planned rejection counts like a stored one, and nothing is written."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = (await evidence_fixture(kb, 1))[0]
        (staging,) = await candidates(kb, STAGING)
        rejection = {"id": staging, "ownership": "rejected", "evidence_add": [evidence]}
        batch = [rejection, {**subdomain("www.acme-staging.net"), "ownership": "candidate"}]
        with pytest.raises(RejectedIdentityError, match=r"^REJECTED_IDENTITY$") as refused:
            await kb.write(WriteRequest.model_validate({"nodes": batch}))
        assert rejected_items(refused) == [("nodes[1]", staging)]
        assert await node_count(kb) == 1
        assert (await stored_state(kb, staging))[0] == "candidate"


async def test_a_scanner_batch_with_rejected_names_is_refused_once_listing_every_one(tmp_path):
    """R26: both rejected names are reported in one error; without them the batch is accepted."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = (await evidence_fixture(kb, 1))[0]
        first, second = await candidates(kb, subdomain("old1.acme.com"), subdomain("old2.acme.com"))
        await reject(kb, first, evidence)
        await reject(kb, second, evidence)
        names = ("old1.acme.com", "new.acme.com", "old2.acme.com")
        batch = [{**ACME, "ownership": "candidate"}, *({**subdomain(name), "ownership": "candidate"} for name in names)]
        with pytest.raises(RejectedIdentityError, match=r"^REJECTED_IDENTITY$") as refused:
            await kb.write(
                WriteRequest.model_validate({"nodes": batch, "relations": [has_subdomain(0, i) for i in (1, 2, 3)]})
            )
        assert rejected_items(refused) == [("nodes[1]", first), ("nodes[3]", second)]
        accepted = await kb.write(
            WriteRequest.model_validate({"nodes": [batch[0], batch[2]], "relations": [has_subdomain(0, 1)]})
        )
        assert [node["created"] for node in accepted["nodes"]] == [True, True]


# A candidate target and the edge properties for each relation an owned name relies on.
RELIANCE = {
    "backed_by_bucket": ({"type": "storage_bucket", "properties": {"provider": "aws_s3", "name": "acme-assets"}}, {}),
    "cname_to": (subdomain("shops.myshopify.com"), {}),
    "dname_to": (subdomain("zone.dnshost.net"), {}),
    "federates_with": ({"type": "identity_tenant", "properties": {"provider": "okta", "tenant_id": "dev-12345"}}, {}),
    "has_mail_exchange": (subdomain("mx.mailhost.net"), {"preference": 10}),
    "has_nameserver": (subdomain("ns1.dnshost.net"), {}),
    "has_soa_primary": (subdomain("ns0.dnshost.net"), {}),
    "has_srv_target": (
        subdomain("sip.voiphost.net"),
        {"service": "_sip", "protocol": "_tcp", "port": 5060, "priority": 10, "weight": 5},
    ),
    "has_svcb_binding": (subdomain("edge.cdnhost.net"), {"record_type": "https", "priority": 1, "alpn": ["h2"]}),
    "hosted_on": (
        {
            "type": "cloud_resource",
            "properties": {"service": "aws_cloudfront", "hostname": "d111111abcdef8.cloudfront.net"},
        },
        {},
    ),
    "resolves_to": (HOST, {}),
}


async def relied_upon(kb, relation, ownership, evidence, source="shop.acme.com"):
    """Write a source name with `ownership` relying on a candidate over `relation`; return both IDs and the edge's."""
    target, properties = RELIANCE[relation]
    claim = {"ownership": ownership, "evidence_add": [] if ownership == "candidate" else [evidence]}
    written = await kb.write(
        WriteRequest.model_validate(
            {
                "nodes": [{**subdomain(source), **claim}, {**target, "ownership": "candidate"}],
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
    return [node["id"] for node in written["nodes"]], written["relations"][0]["id"]


def rejecting(identifier, evidence):
    return {"id": identifier, "ownership": "rejected", "evidence_add": [evidence]}


def test_every_reliance_relation_is_a_catalog_relation_with_a_case_here():
    assert set(RELIANCE) == set(json.loads(_RELIANCE_RELATIONS))
    assert set(RELIANCE) <= set(catalog_module.catalog_view()["relations"])


@pytest.mark.parametrize("ownership", ["owned", "dependency"])
@pytest.mark.parametrize("relation", sorted(RELIANCE))
async def test_rejecting_a_candidate_an_owned_name_relies_on_is_refused_naming_the_relation(
    tmp_path, relation, ownership
):
    """AE10, R23: a relied-upon edge from an owned or dependency name makes the candidate a
    dependency, not a stranger."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = (await evidence_fixture(kb, 1))[0]
        (_source, target), edge = await relied_upon(kb, relation, ownership, evidence)
        with pytest.raises(RecordConflictError, match=r"^REJECTION_BLOCKED$") as refused:
            await kb.write(WriteRequest.model_validate({"nodes": [rejecting(target, evidence)]}))
        blocker = refused.value.details.blocking_record
        assert (blocker.kind, str(blocker.id)) == ("relations", edge)
        assert (await stored_state(kb, target))[0] == "candidate"


async def test_the_sources_planned_ownership_decides_whether_a_rejection_is_blocked(tmp_path):
    """R23: a candidate source does not block; one promoted in the rejecting batch does, and one
    withdrawn to candidate in that batch does not."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = (await evidence_fixture(kb, 1))[0]
        (source, shopify), _edge = await relied_upon(kb, "cname_to", "candidate", evidence)
        promotion = {"id": source, "ownership": "owned", "evidence_add": [evidence]}
        with pytest.raises(RecordConflictError, match=r"^REJECTION_BLOCKED$"):
            await kb.write(WriteRequest.model_validate({"nodes": [promotion, rejecting(shopify, evidence)]}))
        assert (await stored_state(kb, source))[0] == "candidate"
        assert (await reject(kb, shopify, evidence))["ownership"] == "rejected"
        (owner, nameserver), _edge = await relied_upon(kb, "has_nameserver", "owned", evidence, "mail.acme.com")
        withdrawal = {"id": owner, "ownership": "candidate"}
        result = await kb.write(WriteRequest.model_validate({"nodes": [withdrawal, rejecting(nameserver, evidence)]}))
        assert [node["ownership"] for node in result["nodes"]] == ["candidate", "rejected"]


async def test_a_rejection_cannot_share_a_batch_with_its_relations_or_descendants(tmp_path):
    """R24: rejecting X while adding a relation to X, or patching X's port by ID, writes nothing;
    once X is rejected, a new relation to it or a new child under it is refused too."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = (await evidence_fixture(kb, 1))[0]
        written = await kb.write(
            WriteRequest.model_validate(
                {
                    "nodes": [{**HOST, "ownership": "candidate"}, port(443)],
                    "relations": [open_port({"node_index": 0}, 1)],
                }
            )
        )
        host, https = [node["id"] for node in written["nodes"]]
        rejection = {"id": host, "ownership": "rejected", "evidence_add": [evidence]}

        def resolution(source):
            return {
                "type": "resolves_to",
                "source_ref": {"node_index": source},
                "target_ref": {"id": host},
                "properties": {},
            }

        before = await node_count(kb)
        with pytest.raises(ConflictError, match=r"^relations\[0\]: .*rejected"):
            await kb.write(
                WriteRequest.model_validate(
                    {"nodes": [rejection, {**ACME, "ownership": "candidate"}], "relations": [resolution(1)]}
                )
            )
        with pytest.raises(ConflictError, match=r"^nodes\[1\]: .*rejected"):
            await kb.write(
                WriteRequest.model_validate({"nodes": [rejection, {"id": https, "properties": {"banner": "x"}}]})
            )
        assert await node_count(kb) == before
        assert (await stored_state(kb, host))[0] == "candidate"
        await reject(kb, host, evidence)
        with pytest.raises(ConflictError, match=r"^relations\[0\]: .*rejected"):
            await kb.write(
                WriteRequest.model_validate(
                    {"nodes": [{**ACME, "ownership": "candidate"}], "relations": [resolution(0)]}
                )
            )
        with pytest.raises(ConflictError, match=r"^nodes\[0\]: .*rejected"):
            await kb.write(
                WriteRequest.model_validate({"nodes": [port(22)], "relations": [open_port({"id": host}, 0)]})
            )


async def test_a_rejected_node_reclassified_as_owned_gets_nothing_back(tmp_path):
    """R20: rejected -> owned by ID with evidence succeeds; stripped properties and purged
    relations and descendants stay gone."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        evidence = (await evidence_fixture(kb, 1))[0]
        written = await kb.write(
            WriteRequest.model_validate(
                {
                    "nodes": [
                        {
                            **HOST,
                            "properties": {"value": "192.0.2.10", "version": 4, "cloud_provider": "aws"},
                            "ownership": "candidate",
                        },
                        port(443),
                    ],
                    "relations": [open_port({"node_index": 0}, 1)],
                }
            )
        )
        host, https = [node["id"] for node in written["nodes"]]
        await reject(kb, host, evidence)
        owned = await kb.write(
            WriteRequest.model_validate({"nodes": [{"id": host, "ownership": "owned", "evidence_add": [evidence]}]})
        )
        assert owned["nodes"][0]["ownership"] == "owned"
        record = await record_of(kb, "nodes", host)
        assert record["properties"] == HOST["properties"]
        assert (await kb.get(GetRequest(kind="nodes", ids=[https])))["missing_ids"] == [https]
        relations = await kb.search(SearchRequest(kind="relations", source_id=host))
        assert relations["items"] == []


async def test_kb_get_reports_effective_state_and_names_the_root_a_scoped_record_inherits_from(tmp_path):
    """R14, KTD7: a service reports its address's ownership and names that address as its root; the
    root reports its allowlist marker, and a stateless type reports no state."""
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        ids = await inventory_graph(kb)
        ssh = await record_of(kb, "nodes", ids["ssh"])
        assert (ssh["ownership"], ssh["authorization"], ssh["state_root_id"]) == ("owned", "out_of_scope", ids["ip"])
        assert ssh["first_seen"] is not None
        assert ssh["last_seen"] is not None
        assert "allowlist_scoped" not in ssh
        root = await record_of(kb, "nodes", ids["allowlisted"])
        assert (root["ownership"], root["authorization"], root["allowlist_scoped"]) == ("owned", "in_scope", True)
        assert "state_root_id" not in root
        rejected = await record_of(kb, "nodes", ids["stranger"])
        assert (rejected["ownership"], rejected["authorization"]) == ("rejected", "unknown")
        assert (
            not {"ownership", "authorization", "allowlist_scoped", "state_root_id"}
            & (await record_of(kb, "nodes", ids["advisory"])).keys()
        )
