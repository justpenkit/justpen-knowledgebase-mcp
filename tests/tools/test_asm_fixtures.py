"""Replay every ASM coverage fixture through the MCP tools into a fresh workspace.

For each source, each output file is ingested as evidence in its redacted form, every write batch is
sent through `kb_write` linked to that evidence, the written records are read back through `kb_get`,
and the stored evidence is read back whole to prove no redacted secret reached it.
"""

import asyncio
import base64
from typing import Any

import pytest
from fastmcp import Client

from justpen_knowledgebase_mcp.app import create_app
from justpen_knowledgebase_mcp.config import ServerConfig

from .. import asm_harness as harness
from . import envelope

pytestmark = pytest.mark.integration

MEDIA_TYPES = {
    "json": "application/json",
    "jsonl": "application/x-ndjson",
    "bbot_jsonl": "application/x-ndjson",
    "xml": "application/xml",
    "kv_text": "text/plain",
}


async def _ingest(client: Client[Any], source: harness.Source, file_name: str) -> str:
    """Ingest the redacted artifact; an accepted job is polled until it completes."""
    job = envelope(
        await client.call_tool(
            "kb_ingest_evidence",
            {
                "text": harness.redacted_artifact(source, file_name),
                "source": source.name,
                "media_type": MEDIA_TYPES[source.files[file_name]],
            },
        )
    )["data"]
    for _attempt in range(200):
        if job["state"] == "completed":
            return job["evidence_id"]
        assert job["state"] in ("queued", "running"), job
        await asyncio.sleep(0.05)
        job = envelope(await client.call_tool("kb_jobs", {"action": "get", "job_id": job["job_id"]}))["data"]
    pytest.fail(f"ingest of {source.name}/{file_name} did not complete")


def _request(batch: dict[str, Any], ids: dict[tuple[int, int], str], evidence_id: str) -> dict[str, Any]:
    """Substitute cross-batch refs with stored ids and link every record to the file's evidence."""
    request: dict[str, Any] = {
        "nodes": [{**node, "evidence_add": [evidence_id]} for node in batch["request"].get("nodes", [])],
        "relations": [],
    }
    for relation in batch["request"].get("relations", []):
        resolved = {**relation, "evidence_add": [evidence_id]}
        for end in ("source_ref", "target_ref"):
            if "ref" in relation[end]:
                batch_index, node_index = (int(part) for part in relation[end]["ref"].split(":"))
                resolved[end] = {"id": ids[(batch_index, node_index)]}
        request["relations"].append(resolved)
    return request


async def _read_back(client: Client[Any], kind: str, written: list[tuple[str, dict[str, Any]]]) -> None:
    for start in range(0, len(written), 100):
        chunk = written[start : start + 100]
        records = envelope(
            await client.call_tool("kb_get", {"kind": kind, "ids": [identifier for identifier, _ in chunk]})
        )["data"]["records"]
        stored = {record["id"]: record for record in records}
        for identifier, sent in chunk:
            assert stored[identifier]["type"] == sent["type"]
            for name, value in sent["properties"].items():
                assert stored[identifier]["properties"][name] == value, (sent["type"], name)


async def _evidence_bytes(client: Client[Any], evidence_id: str) -> bytes:
    content = b""
    while True:
        page = envelope(
            await client.call_tool(
                "kb_read_evidence",
                {"evidence_id": evidence_id, "offset": len(content), "length": 65536, "format": "base64"},
            )
        )["data"]
        content += base64.b64decode(page["content"])
        if len(content) >= page["total_size"]:
            return content


@pytest.mark.parametrize("name", harness.source_names())
async def test_every_fixture_writes_through_the_tools_and_keeps_secrets_out_of_evidence(tmp_path, name):
    source = harness.load_source(name)
    async with Client(create_app(ServerConfig(workspace_dir=tmp_path))) as client:
        evidence = {file_name: await _ingest(client, source, file_name) for file_name in source.files}
        ids: dict[tuple[int, int], str] = {}
        written: dict[str, list[tuple[str, dict[str, Any]]]] = {"nodes": [], "relations": []}
        for batch_index, batch in enumerate(source.batches):
            request = _request(batch, ids, evidence[batch["file"]])
            result = envelope(await client.call_tool("kb_write", request))["data"]
            for node_index, (ack, sent) in enumerate(zip(result["nodes"], request["nodes"], strict=True)):
                ids[(batch_index, node_index)] = ack["id"]
                written["nodes"].append((ack["id"], sent))
            for ack, sent in zip(result["relations"], request["relations"], strict=True):
                written["relations"].append((ack["id"], sent))
        for kind, records in written.items():
            await _read_back(client, kind, records)
        for file_name, evidence_id in evidence.items():
            stored = await _evidence_bytes(client, evidence_id)
            secrets = harness.redacted_values(source, file_name)
            for secret in secrets:
                assert secret.encode("utf-8") not in stored, (name, file_name)
            if secrets:
                assert harness.REDACTED.encode("utf-8") in stored
