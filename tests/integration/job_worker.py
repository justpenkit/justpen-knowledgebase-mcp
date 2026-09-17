"""Subprocess-only failpoint harness; no production fault controls or runtime switches."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import ConflictError, McpError
from justpen_knowledgebase_mcp.models import DeleteRequest
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage import evidence as evidence_module
from justpen_knowledgebase_mcp.storage.connection import SQLiteRuntime
from justpen_knowledgebase_mcp.storage.evidence import EvidenceStore
from justpen_knowledgebase_mcp.storage.job_retention import JobRetention
from justpen_knowledgebase_mcp.storage.jobs import JobStore
from justpen_knowledgebase_mcp.storage.worker import DatabaseWorkers
from justpen_knowledgebase_mcp.workspace import WorkspacePaths


def barrier(ready, release):
    os.write(ready, b"1")
    os.read(release, 1)


def install_copy_failpoint(scenario, ready, release):
    original_write = evidence_module._write_all
    written = 0

    def write_piece(fd, content):
        nonlocal written
        original_write(fd, content)
        written += len(content)
        if scenario == "partial_copy" and written == 65536:
            barrier(ready, release)

    evidence_module._write_all = write_piece


def install_failpoints(scenario, ready, release):
    install_copy_failpoint(scenario, ready, release)
    original_copy = EvidenceStore.copy_path
    original_publish = EvidenceStore.publish
    original_unlink = EvidenceStore.unlink_blob
    original_stage = EvidenceStore.stage_inline
    stage_count = 0

    if scenario == "race_peer":
        install_peer_claims()

    def copy(self, *args, **kwargs):
        if scenario in ("before_hash", "race", "race_peer"):
            barrier(ready, release)
        return original_copy(self, *args, **kwargs)

    def publish(self, staged):
        original_publish(self, staged)
        if scenario == "published":
            barrier(ready, release)

    def unlink(self, sha256):
        original_unlink(self, sha256)
        if scenario == "unlink":
            barrier(ready, release)

    def stage(self, content, job_id, token):
        nonlocal stage_count
        result = original_stage(self, content, job_id, token)
        stage_count += 1
        if (scenario == "admission" and stage_count == 1) or (scenario == "inline_accepted" and stage_count == 2):
            barrier(ready, release)
        return result

    install_purge_failpoints(scenario, ready, release)
    EvidenceStore.copy_path = copy
    EvidenceStore.publish = publish
    EvidenceStore.unlink_blob = unlink
    EvidenceStore.stage_inline = stage


def install_peer_claims():
    """Force the valid schedule where each process executes its peer's ingest."""
    original_claim = JobStore._claim_selected

    def peer_claim(connection, value, now):
        source = connection.execute("SELECT json_extract(payload,'$.source') FROM jobs WHERE uuid=?", (value,)).get
        if source == str(os.getpid()):
            return None
        return original_claim(connection, value, now)

    JobStore._claim_selected = staticmethod(peer_claim)


async def wait_for_race_jobs(kb, ready):
    """Keep both executors alive until all admitted ingests reach completion."""
    os.write(ready, b"1")  # this process's request is done, its peer's may not be
    job_ids = await kb.workers.read(lambda c, t: [row[0] for row in c.execute("SELECT uuid FROM jobs")])
    assert len(job_ids) == 2
    deadline = time.monotonic() + 60
    for job_id in job_ids:
        result = await kb.job_runner.wait(job_id, deadline)
        assert result["state"] == "completed", result


def install_purge_failpoints(scenario, ready, release):
    original_discard = EvidenceStore.discard_stage

    def discard(self, job_id, token):
        if scenario == "purge_mark":
            barrier(ready, release)
        original_discard(self, job_id, token)
        if scenario == "purge_unlink":
            barrier(ready, release)

    EvidenceStore.discard_stage = discard


async def run(root, scenario, ready, release):
    if scenario in ("retention_retry", "retention_prune"):
        await retention_race(root, scenario, ready, release)
        return
    install_failpoints(scenario, ready, release)
    async with KnowledgeBase.open(ServerConfig(workspace_dir=Path(root))) as kb:
        if scenario in ("purge_mark", "purge_unlink"):
            await asyncio.Event().wait()
        if scenario in ("claim", "stale_claim"):
            await claim_scenario(kb, scenario, ready, release)
            return
        if scenario == "writer":
            await kb.workers.control(lambda c, t: barrier(ready, release))
            return
        if scenario in ("admission", "inline_accepted"):
            result = await kb.ingest_evidence({"base64": "AP8="})
        else:
            request = {"path": "input.bin"}
            if scenario == "race_peer":
                request["source"] = str(os.getpid())
            result = await kb.ingest_evidence(request)
        result = await kb.job_runner.wait(result["job_id"], time.monotonic() + 60)
        if scenario in ("race", "race_peer"):
            await wait_for_race_jobs(kb, ready)
        if scenario == "ready":
            await asyncio.to_thread(barrier, ready, release)
        if scenario == "unlink":
            result = await kb.delete(DeleteRequest(kind="evidence", ids=[result["evidence_id"]]))
        sys.stdout.write(json.dumps(result) + "\n")


async def claim_scenario(kb, scenario, ready, release):
    if scenario == "claim":
        await asyncio.to_thread(barrier, ready, release)
        claim = await kb.workers.control(lambda c, t: JobStore.claim(c, "short", "fixture"))
        result = {"token": claim.token if claim else None}
    else:
        claim = await kb.workers.control(lambda c, t: JobStore.claim(c, "short", "fixture", now=time.time() - 31))
        assert claim is not None
        await asyncio.to_thread(barrier, ready, release)
        try:
            await kb.workers.control(lambda c, t: JobStore.delete_step(c, claim))
        except ConflictError:
            result = {"fenced": True}
        else:
            result = {"fenced": False}
    sys.stdout.write(json.dumps(result) + "\n")


async def retention_race(root, scenario, ready, release):
    config = ServerConfig(workspace_dir=Path(root))
    with WorkspacePaths(config) as workspace, SQLiteRuntime(workspace, config) as factory:
        workers = DatabaseWorkers(factory)
        await workers.start()
        try:
            await asyncio.to_thread(barrier, ready, release)
            os.write(ready, b"1")  # contender is about to enter the real guarded writer

            def transition(connection, _token):
                try:
                    job_id = connection.execute("select uuid from jobs limit 1").get
                    if scenario == "retention_retry":
                        result = JobStore.retry(connection, job_id or "missing")
                    else:
                        result = JobRetention.batch(connection, factory.guard.policy(connection), (0.0, 0))
                except McpError as exc:
                    result = {"error": exc.error_type}
                barrier(ready, release)  # mutation is still uncommitted and holds the writer
                return result

            result = await workers.control(transition)
            sys.stdout.write(json.dumps(result) + "\n")
        finally:
            await workers.close()


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])))
