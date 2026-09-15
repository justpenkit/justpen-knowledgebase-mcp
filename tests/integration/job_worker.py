"""Subprocess-only failpoint harness; no production fault controls or runtime switches."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import ConflictError
from justpen_knowledgebase_mcp.models import DeleteRequest
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.evidence import EvidenceStore
from justpen_knowledgebase_mcp.storage.jobs import JobStore


def barrier(ready, release):
    os.write(ready, b"1")
    os.read(release, 1)


def install_failpoints(scenario, ready, release):
    original_copy = EvidenceStore.copy_path
    original_publish = EvidenceStore.publish
    original_unlink = EvidenceStore.unlink_blob
    original_stage = EvidenceStore.stage_inline
    stage_count = 0

    def copy(self, *args, **kwargs):
        if scenario in ("before_hash", "race"):
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

    EvidenceStore.copy_path = copy
    EvidenceStore.publish = publish
    EvidenceStore.unlink_blob = unlink
    EvidenceStore.stage_inline = stage


async def run(root, scenario, ready, release):
    install_failpoints(scenario, ready, release)
    async with KnowledgeBase.open(ServerConfig(workspace_dir=Path(root))) as kb:
        if scenario in ("claim", "stale_claim"):
            await claim_scenario(kb, scenario, ready, release)
            return
        if scenario == "writer":
            await kb.workers.control(lambda c, t: barrier(ready, release))
            return
        if scenario in ("admission", "inline_accepted"):
            result = await kb.ingest_evidence({"base64": "AP8="})
        else:
            result = await kb.ingest_evidence({"path": "input.bin"})
        result = await kb.job_runner.wait(result["job_id"], time.monotonic() + 60)
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


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])))
