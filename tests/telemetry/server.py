"""Controlled call/job blockers around the actual CLI and ownership lifecycle."""

import asyncio
import os
from pathlib import Path

from justpen_knowledgebase_mcp import __main__ as entry
from justpen_knowledgebase_mcp.jobs import JobRunner

BLOCK_INPUT = "block-value"
FAIL_INPUT = "fail-value"
original_factory = entry.create_app


def factory(config, **kwargs):
    app = original_factory(config, **kwargs)
    if os.environ.get("KB_TELEMETRY_HOLD_JOBS"):
        return app
    arrived = []
    barrier = asyncio.Event()

    @app.tool(name="kb_status")
    async def controlled(secret: str = "") -> dict[str, object]:
        if secret == FAIL_INPUT:
            raise ValueError("sentinel-secret")
        if secret == BLOCK_INPUT:
            await asyncio.to_thread(Path(os.environ["KB_TELEMETRY_TEST_MARKER"]).write_text, "entered")
            await asyncio.Event().wait()
        arrived.append(secret)
        if len(arrived) == 2:
            barrier.set()
        await asyncio.wait_for(barrier.wait(), 5)
        return {"status": "ok", "data": {"arrived": list(arrived)}}

    return app


async def hold_jobs(self, lane, category):
    return False


if os.environ.get("KB_TELEMETRY_HOLD_JOBS"):
    JobRunner._category_step = hold_jobs
entry.create_app = factory
entry.cli()
