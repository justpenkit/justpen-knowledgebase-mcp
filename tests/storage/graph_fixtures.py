"""Graph write shapes, evidence and durable-job fixture admission shared by the test suites."""

import hashlib
import json
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
