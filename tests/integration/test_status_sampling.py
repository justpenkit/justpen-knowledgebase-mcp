"""The allocation sampler releases the sole SQLite reader after its budget."""

import asyncio
import threading
import time

import pytest

from justpen_knowledgebase_mcp import status
from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.status import sample_derived_storage

pytestmark = pytest.mark.integration


async def test_timed_out_derived_scan_preserves_status_and_reader_capacity(tmp_path, monkeypatch):
    started = threading.Event()

    def slow_allocation_scan(connection, token):
        sample_derived_storage(connection, token)
        started.set()
        return connection.execute(
            "WITH RECURSIVE numbers(value) AS (SELECT 1 UNION ALL "
            "SELECT value + 1 FROM numbers WHERE value < 1000000000) "
            "SELECT sum(value) FROM numbers"
        ).get

    monkeypatch.setattr(status, "sample_derived_storage", slow_allocation_scan)
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path, db_reader_threads=1)) as kb:
        assert await asyncio.to_thread(started.wait, 2)
        during = await asyncio.wait_for(kb.status(), 0.5)
        assert during["database"]["available"] is True
        assert during["database"]["stale"] is False
        assert during["database"]["sample"]["derived_storage"]["stale"] is True

        deadline = time.monotonic() + 3
        while True:
            after = await kb.status()
            if after["database"]["sample"]["derived_storage"]["last_error"] == "LIMIT":
                break
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)

        assert after["database"]["stale"] is False
        assert after["database"]["sample"]["derived_storage"]["cached_at"] is None
        assert after["database"]["sample"]["derived_storage"]["text_projection_bytes"] is None
        assert await kb.workers.read(lambda connection, _token: connection.execute("SELECT 7").get) == 7
