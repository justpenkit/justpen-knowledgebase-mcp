"""Real SQLite maintenance of the published evidence coverage aggregate."""

import time
from uuid import uuid4

import pytest

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.evidence import IngestRequest
from justpen_knowledgebase_mcp.models import DeleteRequest
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage import fulltext
from justpen_knowledgebase_mcp.storage.graph_sql import OWNER_DELETE, OWNER_PENDING

pytestmark = pytest.mark.integration

EMPTY = dict.fromkeys(("ready", "pending", "failed", "incomplete", "not_applicable"), 0)


def scanned(connection):
    """Recompute exactly what the replaced per-request `GROUP BY` read returned."""
    counts = dict(EMPTY)
    for state, incomplete, count in connection.execute(fulltext.COVERAGE_SCAN):
        counts["failed" if state == "index_failed" else state] += count
        if incomplete:
            counts["incomplete"] += count
    return counts


async def published(kb):
    """Read the maintained aggregate and the table scan in one snapshot."""
    return await kb.workers.read(lambda c, _t: (fulltext.coverage(c), scanned(c)))


async def test_maintained_coverage_tracks_every_evidence_writer(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        assert await published(kb) == (EMPTY, EMPTY)

        # `EvidenceRecords.publish_record` INSERT, then `claim_item` and `finish_item`
        # as the ingest job indexes the new row.
        text = await kb.ingest_evidence(IngestRequest(text="coverage counter text"))
        stored, scan = await published(kb)
        assert stored == scan
        assert (stored["ready"], stored["incomplete"]) == (1, 0)

        # The `not_applicable` arm of the same INSERT.
        await kb.ingest_evidence({"base64": "AAEC", "media_type": "application/octet-stream"})
        stored, scan = await published(kb)
        assert stored == scan
        assert stored["not_applicable"] == 1

        # `admit_reindex`'s targeted media-type override UPDATE.
        changed = await kb.reindex(
            {"kind": "evidence", "ids": [text["evidence_id"]], "media_type": "application/octet-stream"}
        )
        done = await kb.job_runner.wait(changed["job_id"], time.monotonic() + 10, changed)
        assert done["state"] == "completed"
        stored, scan = await published(kb)
        assert stored == scan
        assert (stored["ready"], stored["not_applicable"]) == (0, 2)

        # `GraphDeletion.prepare`'s delete intent, then `finalize_evidence`'s DELETE.
        deleted = await kb.delete(DeleteRequest(kind="evidence", ids=[text["evidence_id"]]))
        assert deleted["state"] == "completed"
        stored, scan = await published(kb)
        assert stored == scan
        assert stored["not_applicable"] == 1


async def test_delete_intent_leaves_the_aggregate_before_the_purge_removes_the_row(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        raw = await kb.ingest_evidence(IngestRequest(text="purge me"))

        def purge(connection, _token):
            identifier = connection.execute("SELECT id FROM evidence WHERE uuid=?", (raw["evidence_id"],)).get
            connection.execute(OWNER_PENDING["evidence"], (str(uuid4()), 0, 1, identifier))
            intent = (fulltext.coverage(connection), scanned(connection))
            connection.execute("DELETE FROM search_documents WHERE evidence_id=?", (identifier,))
            connection.execute(OWNER_DELETE["evidence"], (identifier,))
            return intent, (fulltext.coverage(connection), scanned(connection))

        intent, removed = await kb.workers.write(purge)
        # A delete intent already leaves the aggregate, so purging the row afterwards
        # must not subtract the same row a second time.
        assert intent == (EMPTY, EMPTY)
        assert removed == (EMPTY, EMPTY)


async def test_startup_reconcile_repairs_a_corrupted_aggregate(tmp_path):
    config = ServerConfig(workspace_dir=tmp_path)
    async with KnowledgeBase.open(config) as kb:
        await kb.ingest_evidence(IngestRequest(text="reconcile me"))
        await kb.workers.write(lambda c, _t: c.execute("UPDATE settings SET evidence_coverage='{}' WHERE singleton=1"))
        stored, scan = await published(kb)
        assert stored != scan
    async with KnowledgeBase.open(config) as kb:
        stored, scan = await published(kb)
        assert stored == scan
        assert stored["ready"] == 1
