"""Evidence text completion, raw references and graph filtering through the facade."""

import asyncio
import base64
import json
import subprocess
import sys

import pytest

from justpen_knowledgebase_mcp import jobs as jobs_module, reindex as indexing_jobs
from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.evidence import IngestRequest, ReadEvidenceRequest
from justpen_knowledgebase_mcp.models import SearchRequest, WriteRequest
from justpen_knowledgebase_mcp.service import KnowledgeBase

pytestmark = pytest.mark.integration


async def test_literal_acceptance_matrix(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        stored = []
        for text in (
            "host=10.0.0.1:443",
            "port 10 retries 0 status 1",
            "1.0.0.10",
            "10-0-0-1",
            "110.0.0.10",
            "10.0.0.1.5",
            "https://admin.example.com/api",
            "CVE 2024 1234",
            "admin",
        ):
            result = await kb.ingest_evidence(IngestRequest(text=text))
            assert result["state"] == "completed"
            assert result["index_state"] == "ready"
            stored.append(result["evidence_id"])
        for query, expected in [
            ("10.0.0.1", stored[:1] + stored[5:6]),
            ("admin.example.com", stored[6:7]),
            ("CVE-2024-1234", []),
            ("Admin", []),
        ]:
            found = await kb.search(SearchRequest(kind="evidence", query=query))
            assert {item["id"] for item in found["items"]} == set(expected)
            assert not found["incomplete"]


async def test_evidence_raw_offsets_failure_and_sources(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        raw = b"\xff\xfe" + "é\r\n𐐀 admin.example.com tail".encode("utf-16le")
        encoded = base64.b64encode(raw).decode()
        result = await kb.ingest_evidence(IngestRequest(base64=encoded, media_type="text/plain", source="one"))
        assert result["state"] == "completed"
        await kb.ingest_evidence(IngestRequest(base64=encoded, media_type="text/plain", source="two"))
        found = await kb.search(SearchRequest(kind="evidence", query="admin.example.com", source="two"))
        ref = found["items"][0]["matches"][0]
        assert ref["byte_start"] == 14
        assert raw[ref["byte_start"] : ref["byte_end"]].decode("utf-16le") == "admin.example.com"
        assert ref["line_start"] == ref["line_end"] == 2
        snippet_range = found["items"][0]["snippet_range"]
        assert snippet_range["byte_start"] == ref["byte_start"]
        assert (
            raw[snippet_range["byte_start"] : snippet_range["byte_end"]].decode("utf-16le")
            == found["items"][0]["snippet"]
        )
        assert snippet_range["byte_end"] > ref["byte_end"]
        failed = await kb.ingest_evidence(IngestRequest(base64="/w==", media_type="text/plain"))
        assert failed["state"] == "failed"
        assert failed["index_state"] == "index_failed"
        read = await kb.read_evidence(ReadEvidenceRequest(evidence_id=failed["evidence_id"], format="base64"))
        assert read["content"] == "/w=="
        empty = await kb.search(SearchRequest(kind="evidence", query="notfound"))
        assert empty["incomplete"]
        assert empty["coverage"]["failed"] == 1


async def test_chunk_boundary_graph_filter_and_words_units(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        nodes = (
            await kb.write(
                WriteRequest.model_validate(
                    {
                        "nodes": [
                            {"type": "domain", "properties": {"name": "a.example", "status": 403, "a": "access"}},
                            {"type": "domain", "properties": {"name": "b.example", "status": 200}},
                        ]
                    }
                )
            )
        )["nodes"]
        body = "x " * 32766 + "boundary phrase " + "z " * 200000 + "secretneedle denied"
        target = {"kind": "nodes", "id": nodes[0]["id"]}
        result = await kb.ingest_evidence(IngestRequest.model_validate({"text": body[:250000], "targets": [target]}))
        # Inline evidence is bounded; use the managed workspace path for the larger body.
        path = tmp_path / "scan.txt"
        path.write_text(body)
        accepted = await kb.ingest_evidence(
            IngestRequest.model_validate({"path": "scan.txt", "media_type": "text/plain", "targets": [target]})
        )
        final = await kb.job_runner.wait(accepted["job_id"], __import__("time").monotonic() + 10, accepted)
        assert final["index_state"] == "ready"
        found = await kb.search(
            SearchRequest(kind="nodes", query="secretneedle", properties={"path": "/status", "op": "eq", "value": 403})
        )
        assert [x["id"] for x in found["items"]] == [nodes[0]["id"]]
        assert not (await kb.search(SearchRequest(kind="nodes", query="secretneedle", include_evidence=False)))["items"]
        assert (await kb.search(SearchRequest(kind="evidence", query="boundary phrase")))["items"]
        assert not (await kb.search(SearchRequest(kind="nodes", query="access denied", query_mode="words")))["items"]
        assert (await kb.search(SearchRequest(kind="evidence", query="boundary denied", query_mode="words")))["items"]
        assert result["state"] == "completed"


async def test_snippet_bounds_and_separate_match_truncation(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        long_literal = "é" * 300
        await kb.ingest_evidence(IngestRequest(text=long_literal))
        found = (await kb.search(SearchRequest(kind="evidence", query=long_literal)))["items"][0]
        assert len(found["snippet"].encode()) <= 512
        assert found["snippet_truncated"]
        assert not found["matches_truncated"]
        assert found["matches"][0]["byte_end"] == 600
        await kb.ingest_evidence(IngestRequest(text="access " + "x " * 1000 + "denied"))
        words = (await kb.search(SearchRequest(kind="evidence", query="access denied", query_mode="words")))["items"][0]
        assert words["snippet_truncated"]
        assert not words["matches_truncated"]
        assert len(words["matches"]) == 2


@pytest.mark.parametrize("capped", [False, True])
async def test_words_snippet_detects_earlier_matches(tmp_path, capped):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        body = "alpha " + "x " * 1000 + "omega " * (32 if capped else 1)
        await kb.ingest_evidence(IngestRequest(text=body))
        item = (await kb.search(SearchRequest(kind="evidence", query="omega alpha", query_mode="words")))["items"][0]
        assert item["snippet_truncated"]
        assert item["matches_truncated"] == capped
        assert item["snippet_range"]["byte_start"] == item["matches"][0]["byte_start"]
        bounds = item["snippet_range"]
        assert body.encode()[bounds["byte_start"] : bounds["byte_end"]].decode() == item["snippet"]
        assert "alpha" not in item["snippet"]


async def test_snippet_checks_later_occurrences_after_reference_cap(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        body = "alpha " * 33 + "x " * 1000 + "alpha"
        await kb.ingest_evidence(IngestRequest(text=body))
        item = (await kb.search(SearchRequest(kind="evidence", query="alpha", query_mode="words")))["items"][0]
        assert len(item["matches"]) == 32
        assert all(ref["byte_end"] <= item["snippet_range"]["byte_end"] for ref in item["matches"])
        assert item["matches_truncated"]
        assert item["snippet_truncated"]


async def test_words_snippet_detects_omitted_other_leaf(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        await kb.write(
            WriteRequest.model_validate(
                {"nodes": [{"type": "domain", "properties": {"name": "a.example", "a": "omega " * 32, "b": "alpha"}}]}
            )
        )
        item = (await kb.search(SearchRequest(kind="nodes", query="omega alpha", query_mode="words")))["items"][0]
        assert len(item["matches"]) == 32
        assert {ref["pointer"] for ref in item["matches"]} == {"/properties/a"}
        assert item["matches_truncated"]
        assert item["snippet_truncated"]
        assert item["snippet_range"]["byte_start"] == item["matches"][0]["byte_start"] == 0


async def test_literal_overlap_deduplicates_and_long_token_leaves_gap(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        body = "x " * 32766 + "boundary phrase " + "z " * 100
        result = await kb.ingest_evidence(IngestRequest(text=body))
        found = (await kb.search(SearchRequest(kind="evidence", query="boundary phrase")))["items"][0]
        assert len(found["matches"]) == 1
        ref = found["matches"][0]
        assert body.encode()[ref["byte_start"] : ref["byte_end"]] == b"boundary phrase"
        path = tmp_path / "long.txt"
        path.write_text("before " + "z" * (1024 * 1024 + 1) + " after")
        accepted = await kb.ingest_evidence(IngestRequest(path="long.txt", media_type="text/plain"))
        done = await kb.job_runner.wait(accepted["job_id"], __import__("time").monotonic() + 10, accepted)
        assert done["index_state"] == "ready"
        assert done["incomplete"]
        assert "TOKEN_TOO_LONG" in done["warnings"]
        assert not (await kb.search(SearchRequest(kind="evidence", query="before after")))["items"]
        assert (await kb.search(SearchRequest(kind="evidence", query="before after", query_mode="words")))["items"]
        assert result["index_state"] == "ready"


async def test_foreground_deadline_then_other_process_indexes_accepted_job(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = jobs_module.index_evidence

    async def hold(runner, claim, evidence_id):
        entered.set()
        await release.wait()
        return await original(runner, claim, evidence_id)

    monkeypatch.setattr(jobs_module, "index_evidence", hold)
    config = ServerConfig(workspace_dir=tmp_path, query_timeout_ms=200)
    async with KnowledgeBase.open(config) as first:
        accepted = await first.ingest_evidence(IngestRequest(text="restart needle"))
        assert entered.is_set()
        assert accepted["status"] == "accepted"
        assert accepted["index_state"] == "pending"
        # Simulate expiration of the durable lease while the old worker is fenced.
        await first.workers.control(
            lambda c, _t: c.execute("UPDATE jobs SET lease_expires_at=0 WHERE uuid=?", (accepted["job_id"],))
        )
        monkeypatch.setattr(jobs_module, "index_evidence", original)
        try:
            script = """
import asyncio, json, sys, time
from pathlib import Path
from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.service import KnowledgeBase
async def main():
    async with KnowledgeBase.open(ServerConfig(workspace_dir=Path(sys.argv[1]))) as kb:
        result = await kb.job_runner.wait(sys.argv[2], time.monotonic() + 5)
        found = await kb.search({"kind": "evidence", "query": "needle"})
        print(json.dumps({"job": result, "found": found}))
asyncio.run(main())
"""
            completed = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-B", "-c", script, str(tmp_path), accepted["job_id"]],
                capture_output=True,
                timeout=10,
                check=True,
            )
            assert completed.stderr == b""
            outcome = json.loads(completed.stdout)
            assert outcome["job"]["state"] == "completed"
            assert outcome["job"]["job_id"] == accepted["job_id"]
            assert outcome["job"]["index_state"] == "ready"
            assert outcome["found"]["items"]
        finally:
            release.set()


async def test_failed_decode_retry_and_duplicate_keep_raw_failure_metadata(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        failed = await kb.ingest_evidence(IngestRequest(base64="/w==", media_type="text/plain"))
        repeated = await kb.ingest_evidence(IngestRequest(base64="/w==", media_type="text/plain", source="again"))
        assert repeated["state"] == "failed"
        assert repeated["evidence_id"] == failed["evidence_id"]
        retry = await kb.jobs({"action": "retry", "job_id": failed["job_id"]})
        done = await kb.job_runner.wait(retry["job_id"], __import__("time").monotonic() + 5, retry)
        assert done["state"] == "failed"
        assert done["evidence_id"] == failed["evidence_id"]
        fixed = await kb.reindex({"kind": "evidence", "ids": [failed["evidence_id"]], "encoding": "latin-1"})
        ready = await kb.job_runner.wait(fixed["job_id"], __import__("time").monotonic() + 5, fixed)
        assert ready["state"] == "completed"
        assert ready["index_state"] == "ready"
        assert (await kb.search(SearchRequest(kind="evidence", query="ÿ")))["items"]


async def test_cancel_after_index_batch_retry_rebuilds_same_raw_blob(tmp_path, monkeypatch):
    original = indexing_jobs._publish_chunk
    cancelled = []

    def cancel_after_batch(connection, claim, owner, chunk, count):
        original(connection, claim, owner, chunk, count)
        if not cancelled:
            cancelled.append(claim.job_id)
            connection.execute("UPDATE jobs SET cancel_requested=1 WHERE uuid=?", (claim.job_id,))

    monkeypatch.setattr(indexing_jobs, "_publish_chunk", cancel_after_batch)
    body = "alpha " + "x " * 200000 + " omega"
    (tmp_path / "cancel.txt").write_text(body)
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        accepted = await kb.ingest_evidence(IngestRequest(path="cancel.txt", media_type="text/plain"))
        stopped = await kb.job_runner.wait(accepted["job_id"], __import__("time").monotonic() + 5, accepted)
        assert stopped["state"] == "cancelled"
        assert stopped["index_state"] == "pending"
        assert stopped["incomplete"]
        before = await kb.workers.read(lambda c, _t: c.execute("SELECT count(*) FROM search_documents").get)
        assert before == 1
        retry = await kb.jobs({"action": "retry", "job_id": stopped["job_id"]})
        done = await kb.job_runner.wait(retry["job_id"], __import__("time").monotonic() + 5, retry)
        assert done["state"] == "completed"
        assert done["evidence_id"] == stopped["evidence_id"]
        found = await kb.search(SearchRequest(kind="evidence", query="alpha omega", query_mode="words"))
        assert len(found["items"]) == 1
        assert not found["incomplete"]


async def test_text_index_completion_keeps_admission_target_warning(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        result = await kb.ingest_evidence(
            {
                "text": "unlinked searchable text",
                "targets": [{"kind": "nodes", "id": "00000000-0000-4000-8000-000000000001"}],
            }
        )
        assert result["state"] == "completed"
        assert result["warnings"] == ["TARGET_NOT_FOUND"]
