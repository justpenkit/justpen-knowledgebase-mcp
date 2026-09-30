"""Graph write shapes, evidence and durable-job fixture admission shared by the test suites."""

import hashlib
import json
import time
from typing import Any
from uuid import uuid4

from justpen_knowledgebase_mcp.catalog import catalog_view
from justpen_knowledgebase_mcp.models import DeleteRequest, Ownership, WriteRequest
from justpen_knowledgebase_mcp.storage.deletions import GraphDeletion


def stated(request: dict[str, Any], ownership: Ownership = "candidate") -> dict[str, Any]:
    """Copy a write request, giving each creating node of a `carries` type an ownership it lacks.

    A test that does not concern inventory state routes its writes through here. ID patches keep
    their state, and scoped children and stateless types never state one, so they pass unchanged.
    """
    if "nodes" not in request:
        return request
    return {**request, "nodes": [_stated_node(node, ownership) for node in request["nodes"]]}


def stated_request(request: dict[str, Any], ownership: Ownership = "candidate") -> WriteRequest:
    """Validate a `stated` copy of the request as the `WriteRequest` storage accepts."""
    return WriteRequest.model_validate(stated(request, ownership))


def _stated_node(node: dict[str, Any], ownership: Ownership) -> dict[str, Any]:
    if "id" in node or "ownership" in node:
        return node
    definition = catalog_view()["nodes"].get(node.get("type"))
    return {**node, "ownership": ownership} if definition and definition["inventory"] == "carries" else node


def graph_node(identifier: int, type_name: str, properties: dict[str, object]) -> dict[str, object]:
    """Build the endpoint row shape consumed by graph structural validation."""
    return {"id": identifier, "type": type_name, "properties": json.dumps(properties)}


def scoped_stack(
    node_order: tuple[str, ...] = ("ip_address", "port", "service"), *, reverse_relations: bool = False
) -> dict[str, list[dict[str, object]]]:
    """Build a catalog-v2 parent-scoped stack with configurable request order."""
    definitions: dict[str, dict[str, object]] = {
        "ip_address": {
            "type": "ip_address",
            "properties": {"value": "192.0.2.10", "version": 4},
            "ownership": "candidate",
        },
        "port": {"type": "port", "properties": {"transport": "tcp", "number": 443}},
        "service": {"type": "service", "properties": {"name": "unknown"}},
    }
    indexes = {name: index for index, name in enumerate(node_order)}
    relations: list[dict[str, object]] = [
        {
            "type": "has_open_port",
            "source_ref": {"node_index": indexes["ip_address"]},
            "target_ref": {"node_index": indexes["port"]},
            "properties": {},
        }
    ]
    if "service" in indexes:
        relations.append(
            {
                "type": "has_service",
                "source_ref": {"node_index": indexes["port"]},
                "target_ref": {"node_index": indexes["service"]},
                "properties": {},
            }
        )
    return {
        "nodes": [definitions[name] for name in node_order],
        "relations": list(reversed(relations)) if reverse_relations else relations,
    }


async def evidence_fixture(kb, count):
    identifiers = ["e_" + hashlib.sha256(str(uuid4()).encode()).hexdigest() for _ in range(count)]

    def create(connection, token):
        connection.executemany(
            "insert into evidence(uuid,sha256,byte_size,blob_path) values (?,?,0,'test')",
            [(value, value[2:]) for value in identifiers],
        )

    await kb.workers.write(create)
    return identifiers


async def admit(kb, kind, identifiers, *, cascade=True):
    job_id = str(uuid4())

    def prepare(connection, token):
        connection.execute(
            "insert into jobs(uuid,kind,state,requested_at,updated_at) values (?,'fixture','queued','now','now')",
            (job_id,),
        )
        return GraphDeletion.prepare(connection, DeleteRequest(kind=kind, ids=identifiers, cascade=cascade), job_id)

    return await kb.workers.write(prepare)


def _scope(relation: str, source: int, target: int) -> dict[str, object]:
    return {
        "type": relation,
        "source_ref": {"node_index": source},
        "target_ref": {"node_index": target},
        "properties": {},
    }


def _host(value: str, evidence: str, **state: object) -> dict[str, object]:
    return {
        "type": "ip_address",
        "properties": {"value": value, "version": 4},
        "ownership": "owned",
        "authorization": "in_scope",
        "evidence_add": [evidence],
        **state,
    }


def _port(number: int, **state: object) -> dict[str, object]:
    return {"type": "port", "properties": {"transport": "tcp", "number": number}, **state}


def _service(name: str, **properties: object) -> dict[str, object]:
    return {"type": "service", "properties": {"name": name, **properties}}


async def inventory_graph(kb: Any) -> dict[str, str]:
    """Write AE7, AE11, a candidate, a rejected candidate and a stateless node; name their IDs.

    `ip` is in scope with port 22 narrowed out (an SSH service and a finding under it) and port 443
    with its service. `allowlisted` is an allowlist-scoped host with 80 and 443 widened, each with a
    service, and a port 8080 with a service written later. `stranger` is rejected after its subdomain
    relation to `www` was written.
    """
    evidence = (await evidence_fixture(kb, 1))[0]
    widened = {"authorization": "in_scope", "evidence_add": [evidence]}
    finding = {
        "type": "finding",
        "properties": {"rule": "nuclei:ssh-weak-cipher", "matcher": "", "title": "Weak SSH cipher", "severity": "low"},
    }
    names = (
        "ip", "port_22", "port_443", "ssh", "https", "finding",
        "allowlisted", "port_80", "allowlisted_443", "http", "allowlisted_https",
        "candidate", "stranger", "www", "cve",
    )  # fmt: skip
    written = await kb.write(
        WriteRequest.model_validate(
            {
                "nodes": [
                    _host("192.0.2.10", evidence),
                    _port(22),
                    _port(443),
                    _service("ssh"),
                    _service("http", secure=True),
                    finding,
                    _host("192.0.2.20", evidence, allowlist_scoped=True),
                    _port(80, **widened),
                    _port(443, **widened),
                    _service("http", secure=False),
                    _service("http", secure=True),
                    {"type": "domain", "properties": {"value": "acme.example"}, "ownership": "candidate"},
                    {"type": "domain", "properties": {"value": "stranger.example"}, "ownership": "candidate"},
                    {"type": "subdomain", "properties": {"value": "www.stranger.example"}, "ownership": "candidate"},
                    {"type": "cve", "properties": {"value": "CVE-2026-1234"}},
                ],
                "relations": [
                    _scope("has_open_port", 0, 1),
                    _scope("has_open_port", 0, 2),
                    _scope("has_service", 1, 3),
                    _scope("has_service", 2, 4),
                    _scope("has_finding", 3, 5),
                    _scope("has_open_port", 6, 7),
                    _scope("has_open_port", 6, 8),
                    _scope("has_service", 7, 9),
                    _scope("has_service", 8, 10),
                    _scope("has_subdomain", 12, 13),
                ],
            }
        )
    )
    ids: dict[str, str] = dict(zip(names, (node["id"] for node in written["nodes"]), strict=True))
    await kb.write(
        WriteRequest.model_validate(
            {"nodes": [{"id": ids["port_22"], "authorization": "out_of_scope", "evidence_add": [evidence]}]}
        )
    )
    later = await kb.write(
        WriteRequest.model_validate(
            {
                "nodes": [_port(8080), _service("http-proxy", secure=False)],
                "relations": [
                    {
                        "type": "has_open_port",
                        "source_ref": {"id": ids["allowlisted"]},
                        "target_ref": {"node_index": 0},
                        "properties": {},
                    },
                    _scope("has_service", 0, 1),
                ],
            }
        )
    )
    ids["port_8080"], ids["alternate"] = (node["id"] for node in later["nodes"])
    rejected = await kb.write(
        WriteRequest.model_validate(
            {"nodes": [{"id": ids["stranger"], "ownership": "rejected", "evidence_add": [evidence]}]}
        )
    )
    job = await kb.job_runner.wait(rejected["nodes"][0]["rejection_job_id"], time.monotonic() + 5)
    assert job["state"] == "completed"
    return ids
