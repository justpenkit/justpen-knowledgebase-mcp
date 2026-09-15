"""Reusable installed-wheel consumer flow using every public tool over stdio."""

import asyncio
import json
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

TOOLS = {
    "kb_status",
    "kb_types",
    "kb_write",
    "kb_get",
    "kb_search",
    "kb_neighbors",
    "kb_delete",
    "kb_ingest_evidence",
    "kb_read_evidence",
    "kb_jobs",
    "kb_reindex",
}


async def exercise(python, workspace):
    transport = StdioTransport(
        command=str(python),
        args=["-B", "-m", "justpen_knowledgebase_mcp"],
        env={
            "JUSTPEN_KNOWLEDGEBASE_WORKSPACE_DIR": str(workspace),
            "PYTHONDONTWRITEBYTECODE": "1",
            "FASTMCP_SHOW_SERVER_BANNER": "false",
        },
    )
    called = set()
    async with Client(transport) as client:
        assert {tool.name for tool in await client.list_tools()} == TOOLS

        async def call(name, arguments):
            result = await client.call_tool(name, arguments)
            value = result.structured_content
            assert value is not None
            assert value["status"] == "ok", value
            called.add(name)
            return value["data"]

        await call("kb_status", {})
        await call("kb_types", {"kind": "nodes", "type": "hostname"})
        result = await call("kb_write", {"nodes": [{"type": "hostname", "properties": {"name": "consumer.example"}}]})
        identifier = result["nodes"][0]["id"]
        assert (await call("kb_get", {"kind": "nodes", "ids": [identifier]}))["records"][0]["id"] == identifier
        assert (await call("kb_neighbors", {"seed_ids": [identifier]}))["nodes"][0]["id"] == identifier
        job = await call(
            "kb_ingest_evidence", {"text": "consumer proof needle", "targets": [{"kind": "nodes", "id": identifier}]}
        )
        assert job["state"] == "completed", job
        assert (await call("kb_read_evidence", {"evidence_id": job["evidence_id"]}))[
            "content"
        ] == "consumer proof needle"
        assert (await call("kb_jobs", {"action": "get", "job_id": job["job_id"]}))["state"] == "completed"
        assert (await call("kb_search", {"kind": "nodes", "query": "needle"}))["items"][0]["id"] == identifier
        rebuild = await call("kb_reindex", {"kind": "evidence", "ids": [job["evidence_id"]]})
        state = rebuild
        for _ in range(200):
            state = await call("kb_jobs", {"action": "get", "job_id": rebuild["job_id"]})
            if state["state"] == "completed":
                break
            assert state["state"] in {"queued", "running"}, state
            await asyncio.sleep(0.01)
        assert state["state"] == "completed"
        await call("kb_delete", {"kind": "nodes", "ids": [identifier], "cascade": True})
        assert (await call("kb_get", {"kind": "nodes", "ids": [identifier]}))["missing_ids"] == [identifier]
    assert called == TOOLS
    return sorted(called)


if __name__ == "__main__":
    print(json.dumps({"tools": asyncio.run(exercise(sys.executable, Path(sys.argv[1])))}))
