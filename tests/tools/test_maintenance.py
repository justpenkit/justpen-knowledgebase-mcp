"""Cached status and real job tools, never health claims from discovery."""

import pytest
from fastmcp import Client

from justpen_knowledgebase_mcp.app import create_app
from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import BusyError
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.status import StatusSampler

from . import envelope

pytestmark = pytest.mark.integration


async def test_status_reports_effective_http_hosts(tmp_path):
    config = ServerConfig(
        workspace_dir=tmp_path,
        transport="http",
        host="127.0.0.1",
        allowed_hosts=("kb.example.test", "192.0.2.4"),
    )
    async with KnowledgeBase.open(config) as kb:
        result = await kb.status()
    assert result["allowed_hosts"] == ["127.0.0.1", "kb.example.test", "192.0.2.4"]


async def test_status_is_sql_free_and_keeps_stale_snapshot(kb, monkeypatch):
    first = await kb.status()
    assert first["database"]["available"] is True
    assert first["database"]["sample"]["schema_version"] == 3
    assert first["database"]["sample"]["index_coverage"]["ready"] == 0

    async def unavailable(*_args, **_kwargs):
        raise BusyError("database queue unavailable")

    monkeypatch.setattr(kb.workers, "read", unavailable)
    await kb.status_sampler.refresh()
    result = await kb.status()
    assert result["database"]["stale"] is True
    assert result["database"]["available"] is True
    assert result["database"]["sample"] == first["database"]["sample"]
    assert result["wal"]["estimated_completion_ms"] is None
    assert result["retention"]["available"] is True
    assert result["capabilities"]["shutdown_grace_ms"] == 30000
    assert result["io_queues"]["short"]["capacity"] == 32


async def test_status_preserves_reset_and_retry_advice(kb):
    kb.workers.factory.status_cache.reset(active=True)
    result = await kb.status()
    assert result["wal"]["phase"] == "reset"
    assert result["wal"]["retry_after_ms"] >= 1000
    assert result["authentication"] == "none"
    assert result["canonical_session_source"] == "JUSTPEN_SESSION_ID"
    assert result["request_workspace_selection"] is False


async def test_job_request_presence_and_reindex(tmp_path):
    async with Client(create_app(ServerConfig(workspace_dir=tmp_path))) as client:
        ingest = await client.call_tool("kb_ingest_evidence", {"text": "evidence words"})
        job = envelope(ingest)["data"]
        result = await client.call_tool("kb_jobs", {"action": "get", "job_id": job["job_id"]})
        assert envelope(result)["data"]["job_id"] == job["job_id"]
        invalid = await client.call_tool(
            "kb_jobs", {"action": "get", "job_id": job["job_id"], "limit": 20}, raise_on_error=False
        )
        assert invalid.is_error
        assert envelope(invalid)["error"].startswith("INVALID:")
        result = await client.call_tool("kb_reindex", {"kind": "evidence", "ids": [job["evidence_id"]]})
        assert envelope(result)["data"]["status"] == "accepted"


@pytest.mark.parametrize(
    ("phase", "reason"),
    [
        ("normal", None),
        ("pressure", "WAL_PRESSURE"),
        ("assessment_pending", "WAL_PRESSURE"),
        ("unknown", "WAL_PRESSURE"),
        ("reset", "RESET_PENDING"),
    ],
)
async def test_status_reports_cached_admission_reason(kb, monkeypatch, phase, reason):
    cached = kb.maintenance.status()
    monkeypatch.setattr(kb.maintenance, "status", lambda: {**cached, "phase": phase})
    result = await kb.status()
    assert result["wal"]["reason"] == reason
    assert result["wal"]["phase"] == phase
    assert result["wal"]["unavailable"] == cached["unavailable"]


async def test_status_initial_sampling_failure_is_unknown(kb, monkeypatch):

    async def unavailable(*_args, **_kwargs):
        raise BusyError("database queue unavailable")

    sampler = StatusSampler(kb.workers)
    monkeypatch.setattr(kb.workers, "read", unavailable)
    await sampler.refresh()
    assert sampler.snapshot()["sample"] is None
    assert sampler.snapshot()["available"] is False
    assert sampler.snapshot()["stale"] is True
    assert sampler.snapshot()["last_error"] == "BUSY"


async def test_status_samples_aggregate_coverage_and_property_fallback(kb):
    await kb.write(
        {
            "nodes": [
                {
                    "type": "domain",
                    "properties": {"value": "wide.example", **{f"key{index}": index for index in range(600)}},
                }
            ]
        }
    )
    imported = await kb.ingest_evidence({"text": "status sample"})
    assert imported["state"] == "completed"
    await kb.status_sampler.refresh()
    result = await kb.status()
    sample = result["database"]["sample"]
    assert sample["index_coverage"]["ready"] == 1
    assert sample["property_index_fallback"]["nodes"] == 1
    assert sample["jobs"]["completed"] == 1
    assert sample["policy"]["wal_high_bytes"] == 256 * 1024**2
