"""Real SQLite canonical record projection and full-text search contracts."""

import asyncio
import time
from uuid import uuid4

import apsw
import pytest

from justpen_knowledgebase_mcp import reindex as indexing_jobs
from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import ConflictError, InvalidParamsError, LimitError
from justpen_knowledgebase_mcp.evidence import IngestRequest
from justpen_knowledgebase_mcp.models import SearchRequest, WriteRequest
from justpen_knowledgebase_mcp.mutations import canonical_json
from justpen_knowledgebase_mcp.reindex import ReindexRequest, admit_reindex
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage import fulltext
from justpen_knowledgebase_mcp.storage.fulltext import append_chunk, claim_item, clear_item_batch, finish_item
from justpen_knowledgebase_mcp.storage.jobs import JobStore
from justpen_knowledgebase_mcp.text import TextChunk

pytestmark = pytest.mark.integration


async def test_records_literal_words_and_full_projection(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        written = await kb.write(
            WriteRequest.model_validate(
                {
                    "nodes": [
                        {
                            "type": "domain",
                            "label": "Admin",
                            "source": "hiddenword",
                            "properties": {
                                "name": "admin.example.com",
                                "a": "access",
                                "b": "denied",
                                "long": "x " * 2000 + "secretword",
                                "items": ["leaf"] * 600 + ["lateword"],
                            },
                        }
                    ]
                }
            )
        )
        identifier = written["nodes"][0]["id"]
        for query, mode, matches in [
            ("Admin", "literal", True),
            ("admin", "literal", True),
            ("ADMIN", "literal", False),
            ("access denied", "literal", False),
            ("access denied", "words", True),
            ("hiddenword", "words", False),
            ("secretword", "literal", True),
            ("lateword", "literal", True),
        ]:
            found = await kb.search(SearchRequest.model_validate({"kind": "nodes", "query": query, "query_mode": mode}))
            assert [item["id"] for item in found["items"]] == ([identifier] if matches else [])
        await kb.write(WriteRequest.model_validate({"nodes": [{"id": identifier, "properties": {"a": "removed"}}]}))
        assert not (await kb.search(SearchRequest(kind="nodes", query="access")))["items"]

        def integrity(c, _t):
            c.execute("INSERT INTO search_fts(search_fts,rank) VALUES('integrity-check',1)")
            return c.execute("SELECT count(*) FROM search_documents").get, c.execute(
                "SELECT count(*) FROM search_fts_docsize"
            ).get

        counts = await kb.workers.write(integrity)
        assert counts[0] == counts[1] == 606


async def test_reindex_full_slot_epoch_and_current_record(tmp_path):

    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        written = await kb.write(
            WriteRequest.model_validate(
                {"nodes": [{"type": "domain", "properties": {"name": "a.example", "note": "current"}}]}
            )
        )

        def admission(c, _t):
            before = c.execute("SELECT query_epoch FROM settings").get
            one = admit_reindex(c, ReindexRequest(kind="nodes", all=True), str(uuid4()))
            two = admit_reindex(c, ReindexRequest(kind="nodes", all=True), str(uuid4()))
            assert one["job_id"] == two["job_id"]
            assert two["reused"]
            assert c.execute("SELECT query_epoch FROM settings").get == before + 1
            with pytest.raises(apsw.ConstraintError):
                JobStore.insert(c, str(uuid4()), "reindex", "bulk", {"kind": "evidence", "all": True})
            with pytest.raises(ConflictError):
                admit_reindex(c, ReindexRequest(kind="evidence", all=True), str(uuid4()))
            JobStore.cancel(c, one["job_id"])
            assert JobStore.get(c, one["job_id"])["state"] == "queued"
            with pytest.raises(ConflictError):
                admit_reindex(c, ReindexRequest(kind="relations", all=True), str(uuid4()))
            return one

        one = await kb.workers.write(admission)
        final = await kb.job_runner.wait(one["job_id"], __import__("time").monotonic() + 10, one)
        assert final["state"] == "cancelled"
        result = await kb.reindex({"kind": "nodes", "ids": [written["nodes"][0]["id"]]})
        done = await kb.job_runner.wait(result["job_id"], __import__("time").monotonic() + 10, result)
        assert done["state"] == "completed"
        assert (await kb.search(SearchRequest(kind="nodes", query="current")))["items"]


async def test_evidence_override_preserves_bytes_and_fences_owner(tmp_path):

    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        raw = await kb.ingest_evidence(IngestRequest(base64="aGVsbG8="))
        identifier = raw["evidence_id"]

        def race(c, _t):
            job = str(uuid4())
            JobStore.insert(c, job, "reindex", "bulk", {"kind": "evidence", "ids": [identifier], "all": False})
            claim = JobStore.claim(c, "bulk", "reindex")
            assert claim is not None
            owner = claim_item(c, identifier, c.execute("SELECT id FROM jobs WHERE uuid=?", (job,)).get, claim.token)
            assert clear_item_batch(c, owner)
            override = admit_reindex(
                c, ReindexRequest(kind="evidence", ids=[identifier], media_type="text/plain"), str(uuid4())
            )
            for callback in (
                lambda: clear_item_batch(c, owner),
                lambda: append_chunk(c, owner, TextChunk("stale", "utf-8", 0, 1, 0)),
                lambda: finish_item(c, owner, "ready", incomplete=False),
            ):
                with pytest.raises(ConflictError, match="GENERATION"):
                    callback()
            JobStore.finish(c, claim, "completed", {})
            return override

        result = await kb.workers.write(race)
        done = await kb.job_runner.wait(result["job_id"], __import__("time").monotonic() + 10, result)
        assert done["state"] == "completed"
        assert (await kb.search(SearchRequest(kind="evidence", query="hello")))["items"][0]["id"] == identifier
        changed = await kb.reindex({"kind": "evidence", "ids": [identifier], "media_type": "application/octet-stream"})
        done = await kb.job_runner.wait(changed["job_id"], __import__("time").monotonic() + 10, changed)
        assert done["state"] == "completed"
        assert not (await kb.search(SearchRequest(kind="evidence", query="hello")))["items"]


async def test_full_generation_change_after_cleanup_skips_then_continues(tmp_path, monkeypatch):
    entered, resume = asyncio.Event(), asyncio.Event()
    blocked_runners = []
    original = indexing_jobs._index_stream

    async def barrier(runner, claim, owner):
        if claim.payload.get("all") and not entered.is_set():
            blocked_runners.append(runner)
            entered.set()
            await resume.wait()
        return await original(runner, claim, owner)

    monkeypatch.setattr(indexing_jobs, "_index_stream", barrier)
    config = ServerConfig(workspace_dir=tmp_path)
    async with KnowledgeBase.open(config) as first, KnowledgeBase.open(config) as second:
        one = await first.ingest_evidence(IngestRequest(text="first searchable"))
        two = await first.ingest_evidence(IngestRequest(text="second searchable"))
        full = await first.reindex({"kind": "evidence", "all": True})
        try:
            await asyncio.wait_for(entered.wait(), 3)
            other = second if blocked_runners[0] is first.job_runner else first
            override = await other.reindex({"kind": "evidence", "ids": [one["evidence_id"]], "encoding": "utf-8"})
            completed = await other.job_runner.wait(override["job_id"], __import__("time").monotonic() + 5, override)
            assert completed["state"] == "completed"
        finally:
            resume.set()
        done = await first.job_runner.wait(full["job_id"], __import__("time").monotonic() + 5, full)
        assert done["state"] == "completed"
        assert done["coverage_incomplete"]
        assert done["generation_changed_count"] == 1
        assert done["sample_ids"] == [one["evidence_id"]]
        found = await first.search(SearchRequest(kind="evidence", query="searchable"))
        assert {item["id"] for item in found["items"]} == {one["evidence_id"], two["evidence_id"]}
        assert not found["incomplete"]


async def test_full_busy_skip_does_not_release_other_item_owner(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        raw = await kb.ingest_evidence(IngestRequest(text="owned text"))

        def claim_other(c, _t):
            job_id = str(uuid4())
            JobStore.insert(
                c, job_id, "reindex", "bulk", {"kind": "evidence", "ids": [raw["evidence_id"]], "all": False}
            )
            claim = JobStore.claim(c, "bulk", "reindex")
            assert claim is not None
            owner = claim_item(
                c, raw["evidence_id"], c.execute("SELECT id FROM jobs WHERE uuid=?", (job_id,)).get, claim.token
            )
            return claim, owner

        claim, owner = await kb.workers.write(claim_other)
        full = await kb.reindex({"kind": "evidence", "all": True})
        done = await kb.job_runner.wait(full["job_id"], __import__("time").monotonic() + 5, full)
        assert done["state"] == "completed"
        assert done["index_busy_count"] == 1
        assert done["coverage_incomplete"]

        def release_other(c, _t):
            finish_item(c, owner, "ready", incomplete=False)
            JobStore.finish(c, claim, "completed", {})

        await kb.workers.write(release_other)
        assert not (await kb.search(SearchRequest(kind="evidence", query="text")))["incomplete"]


@pytest.mark.parametrize("transition", ["queued", "reclaimed", "cancel_requested"])
async def test_live_override_reservation_survives_job_claim(tmp_path, monkeypatch, transition):
    entered, resume = asyncio.Event(), asyncio.Event()
    held = []
    original = indexing_jobs.index_evidence

    async def barrier(runner, claim, evidence_id):
        if not claim.payload.get("all"):
            held.append((runner, claim))
            entered.set()
            await resume.wait()
        return await original(runner, claim, evidence_id)

    monkeypatch.setattr(indexing_jobs, "index_evidence", barrier)
    config = ServerConfig(workspace_dir=tmp_path)
    async with KnowledgeBase.open(config) as first, KnowledgeBase.open(config) as second:
        raw = await first.ingest_evidence(IngestRequest(text="reserved searchable"))
        identifier = raw["evidence_id"]

        def reserve(c, _t):
            override = admit_reindex(
                c, ReindexRequest(kind="evidence", ids=[identifier], encoding="utf-8"), str(uuid4())
            )
            if transition == "reclaimed":
                previous = JobStore.claim(c, "bulk", "reindex")
                assert previous is not None
                claim_item(
                    c, identifier, c.execute("SELECT id FROM jobs WHERE uuid=?", (previous.job_id,)).get, previous.token
                )
                c.execute("UPDATE jobs SET lease_expires_at=0 WHERE uuid=?", (previous.job_id,))
            return override

        override = await first.workers.write(reserve)
        try:
            await asyncio.wait_for(entered.wait(), 3)
            blocked, claim = held[0]
            other = second if blocked is first.job_runner else first
            if transition == "cancel_requested":
                await other.workers.control(lambda c, _t: JobStore.cancel(c, claim.job_id))

            def reservation(c, _t):
                return c.execute(
                    "SELECT j.uuid,e.index_owner_token,j.lease_token,j.state,j.lease_expires_at "
                    "FROM evidence e JOIN jobs j ON j.id=e.index_owner_job_id WHERE e.uuid=?",
                    (identifier,),
                ).fetchone()

            before = await other.workers.read(reservation)
            assert before[0] == claim.job_id
            assert before[1] != before[2] == claim.token
            assert before[3] == "running"
            assert before[4] > time.time()
            full = await other.reindex({"kind": "evidence", "all": True})
            done = await other.job_runner.wait(full["job_id"], time.monotonic() + 5, full)
            assert done["state"] == "completed"
            assert done.get("index_busy_count") == 1
            assert done["coverage_incomplete"]
            assert done["sample_ids"] == [identifier]
            after = await other.workers.read(reservation)
            assert after[:4] == before[:4]
        finally:
            resume.set()
        completed = await first.job_runner.wait(override["job_id"], time.monotonic() + 5, override)
        assert completed["state"] == ("cancelled" if transition == "cancel_requested" else "completed")
        if transition != "cancel_requested":
            found = await first.search(SearchRequest(kind="evidence", query="searchable"))
            assert found["items"][0]["id"] == identifier
            assert not found["incomplete"]


@pytest.mark.parametrize("owner_state", ["expired", "completed", "failed", "cancelled"])
async def test_inactive_item_reservation_allows_takeover(tmp_path, owner_state):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        raw = await kb.ingest_evidence(IngestRequest(text="recoverable text"))

        def takeover(c, _t):
            old_id, new_id = str(uuid4()), str(uuid4())
            JobStore.insert(c, old_id, "reindex", "bulk", {"kind": "evidence", "ids": [raw["evidence_id"]]})
            old = JobStore.claim(c, "bulk", "reindex")
            assert old is not None
            owner = claim_item(
                c, raw["evidence_id"], c.execute("SELECT id FROM jobs WHERE uuid=?", (old_id,)).get, old.token
            )
            if owner_state == "expired":
                c.execute("UPDATE jobs SET lease_expires_at=0 WHERE uuid=?", (old_id,))
            else:
                JobStore.finish(c, old, owner_state, {})
            JobStore.insert(c, new_id, "reindex", "bulk", {"kind": "evidence", "all": True})
            replacement = claim_item(
                c, raw["evidence_id"], c.execute("SELECT id FROM jobs WHERE uuid=?", (new_id,)).get, new_id
            )
            assert replacement.generation == owner.generation
            with pytest.raises(ConflictError, match="GENERATION"):
                finish_item(c, owner, "ready", incomplete=False)
            finish_item(c, replacement, "ready", incomplete=False)
            c.execute("UPDATE jobs SET state='completed' WHERE uuid IN (?,?)", (old_id, new_id))

        await kb.workers.write(takeover)


async def test_relevance_one_page_and_cursor_query_binding(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        for text in ("needle", "needle other", "other needle third"):
            await kb.ingest_evidence(IngestRequest(text=text))
        ranked = await kb.search(SearchRequest(kind="evidence", query="needle", sort="relevance", limit=2))
        assert len(ranked["items"]) == 2
        assert ranked["has_more"]
        assert ranked["cursor"] is None
        assert [item["score"] for item in ranked["items"]] == sorted(item["score"] for item in ranked["items"])
        page = await kb.search(SearchRequest(kind="evidence", query="needle", limit=1))
        second = await kb.search(SearchRequest(kind="evidence", query="needle", cursor=page["cursor"]))
        assert len(second["items"]) == 2

        with pytest.raises(InvalidParamsError):
            await kb.search({"kind": "evidence", "query": "needle", "query_mode": "words", "cursor": page["cursor"]})
        with pytest.raises(InvalidParamsError):
            await kb.search({"kind": "evidence", "query": "needle", "sort": "relevance", "cursor": page["cursor"]})


async def test_reindex_override_validation_and_failed_full_retry_epoch(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        raw = await kb.ingest_evidence(IngestRequest(base64="AA=="))
        with pytest.raises(InvalidParamsError):
            await kb.reindex({"kind": "evidence", "ids": [raw["evidence_id"]], "encoding": "auto"})
        for request in (
            {"kind": "evidence", "all": True, "media_type": "text/plain"},
            {"kind": "nodes", "all": True, "encoding": "utf-8"},
            {"kind": "evidence", "ids": [raw["evidence_id"]], "media_type": "text/" + "x" * 251},
        ):
            with pytest.raises(InvalidParamsError):
                await kb.reindex(request)

        def full_retry(c, _t):
            before = c.execute("SELECT query_epoch FROM settings").get
            one = admit_reindex(c, ReindexRequest(kind="nodes", all=True), str(uuid4()))
            claim = JobStore.claim(c, "bulk", "reindex")
            assert claim is not None
            JobStore.finish(c, claim, "failed", {})
            JobStore.retry(c, one["job_id"])
            assert c.execute("SELECT query_epoch FROM settings").get == before + 2
            assert admit_reindex(c, ReindexRequest(kind="nodes", all=True), str(uuid4()))["reused"]
            assert c.execute("SELECT query_epoch FROM settings").get == before + 2

        await kb.workers.write(full_retry)


async def test_json_expanded_snippet_budget_and_match_reference_cap(tmp_path):

    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        nodes = [
            {"type": "domain", "properties": {"name": f"n{i}.example", "note": "word" + "\x00" * 1000}}
            for i in range(100)
        ]
        await kb.write(WriteRequest.model_validate({"nodes": nodes}))
        found = await kb.search(SearchRequest(kind="nodes", query="word", limit=100))
        assert len(canonical_json(found).encode()) < 256 * 1024
        assert found["has_more"]
        assert len(found["items"]) < 100
        rest = await kb.search(SearchRequest(kind="nodes", query="word", limit=100, cursor=found["cursor"]))
        assert len(found["items"]) + len(rest["items"]) == 100
        await kb.ingest_evidence(IngestRequest(text="repeat " * 100))
        repeated = (await kb.search(SearchRequest(kind="evidence", query="repeat")))["items"][0]
        assert len(repeated["matches"]) == 32
        assert repeated["matches_truncated"]


async def test_literal_verification_timeout_is_incomplete_limit(tmp_path, monkeypatch):

    original = fulltext._literal_ranges

    def slow(*args):
        time.sleep(0.15)
        yield from original(*args)

    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path, query_timeout_ms=100)) as kb:
        await kb.write(
            WriteRequest.model_validate({"nodes": [{"type": "domain", "properties": {"name": "needle.example"}}]})
        )
        monkeypatch.setattr(fulltext, "_literal_ranges", slow)
        with pytest.raises(LimitError, match="incomplete"):
            await kb.search(SearchRequest(kind="nodes", query="needle"))


async def test_long_canonical_pointer_keeps_one_exact_match_reference(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        key = "/" * 60000
        await kb.write(
            WriteRequest.model_validate(
                {"nodes": [{"type": "domain", "properties": {"name": "a.example", key: "uniqueneedle"}}]}
            )
        )
        found = (await kb.search(SearchRequest(kind="nodes", query="uniqueneedle")))["items"][0]
        assert found["matches"][0]["pointer"] == "/properties/" + "~1" * 60000
        assert found["matches"][0]["byte_end"] == len("uniqueneedle")
        assert found["snippet_range"]["byte_end"] == len("uniqueneedle")
        assert len(canonical_json(found).encode()) < 256 * 1024
        assert "pointer" not in found["snippet_range"]


async def test_record_reindex_reads_canonical_after_concurrent_writer(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        node = (
            await kb.write(
                WriteRequest.model_validate(
                    {"nodes": [{"type": "domain", "properties": {"name": "a.example", "note": "oldvalue"}}]}
                )
            )
        )["nodes"][0]["id"]
        original = kb.workers.write

        async def barrier(callback, *args, **kwargs):
            if callback.__name__ == "refresh":
                entered.set()
                await release.wait()
            return await original(callback, *args, **kwargs)

        monkeypatch.setattr(kb.workers, "write", barrier)
        accepted = await kb.reindex({"kind": "nodes", "ids": [node]})
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await kb.write(WriteRequest.model_validate({"nodes": [{"id": node, "properties": {"note": "newvalue"}}]}))
        finally:
            release.set()
        done = await kb.job_runner.wait(accepted["job_id"], time.monotonic() + 5, accepted)
        assert done["state"] == "completed"
        assert (await kb.search(SearchRequest(kind="nodes", query="newvalue")))["items"]
        assert not (await kb.search(SearchRequest(kind="nodes", query="oldvalue")))["items"]
