"""Integration-only evidence and durable-job fixture admission."""

import hashlib
import json
from uuid import uuid4

from justpen_knowledgebase_mcp.models import DeleteRequest
from justpen_knowledgebase_mcp.storage.deletions import GraphDeletion


def graph_node(identifier: int, type_name: str, properties: dict[str, object]) -> dict[str, object]:
    """Build the endpoint row shape consumed by graph structural validation."""
    return {"id": identifier, "type": type_name, "properties": json.dumps(properties)}


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
