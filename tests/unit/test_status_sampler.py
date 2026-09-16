"""Independent status caches and owned sampling tasks."""

import asyncio
import time
from unittest.mock import Mock

from justpen_knowledgebase_mcp.config import WorkspacePolicy
from justpen_knowledgebase_mcp.errors import LimitError
from justpen_knowledgebase_mcp.status import StatusSampler
from justpen_knowledgebase_mcp.storage.status import sample_derived_storage, sample_status
from justpen_knowledgebase_mcp.storage.worker import OperationToken


def cheap_sample():
    return {
        "schema_version": 1,
        "catalog_version": 1,
        "index_format_version": 1,
        "policy": WorkspacePolicy().model_dump(),
        "jobs": dict.fromkeys(("queued", "running", "completed", "failed", "cancelled"), 0),
        "index_coverage": dict.fromkeys(("ready", "pending", "failed", "incomplete", "not_applicable"), 0),
        "property_index_fallback": {"nodes": 0, "relations": 0},
    }


async def test_initial_cheap_sample_does_not_wait_for_derived_and_close_joins_both_tasks():
    derived_started = asyncio.Event()
    derived_cancelled = asyncio.Event()

    async def read(callback, token=None):
        if callback is sample_status:
            return cheap_sample()
        assert callback is sample_derived_storage
        assert isinstance(token, OperationToken)
        derived_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            derived_cancelled.set()

    sampler = StatusSampler(Mock(read=read))
    await sampler.start()
    await asyncio.wait_for(derived_started.wait(), 1)
    before = sampler.snapshot()
    assert before["available"] is True
    assert before["stale"] is False
    assert before["sample"]["derived_storage"]["available"] is False
    assert before["sample"]["derived_storage"]["reason"] is None
    assert before["sample"]["derived_storage"]["cached_at"] is None
    assert before["sample"]["derived_storage"]["stale"] is True
    await sampler.close()
    assert derived_cancelled.is_set()
    assert sampler._task is not None
    assert sampler._derived_task is not None
    assert sampler._task.done()
    assert sampler._derived_task.done()


async def test_derived_failure_keeps_complete_last_known_bytes_without_staling_cheap_sample():
    derived_calls = 0

    async def read(callback, token=None):
        nonlocal derived_calls
        if callback is sample_status:
            return cheap_sample()
        assert callback is sample_derived_storage
        assert isinstance(token, OperationToken)
        assert 0 < token.deadline - time.monotonic() <= 1
        derived_calls += 1
        if derived_calls == 2:
            raise LimitError("private database detail")
        return {
            "available": True,
            "reason": None,
            "text_projection_bytes": 4096,
            "fts_index_bytes": 8192,
            "property_index_bytes": 12288,
        }

    sampler = StatusSampler(Mock(read=read))
    await sampler.refresh()
    await sampler.refresh_derived()
    first = sampler.snapshot()
    await sampler.refresh_derived()
    second = sampler.snapshot()
    assert second["available"] is True
    assert second["stale"] is False
    assert second["sample"]["derived_storage"]["available"] is True
    assert second["sample"]["derived_storage"]["stale"] is True
    assert second["sample"]["derived_storage"]["last_error"] == "LIMIT"
    assert second["sample"]["derived_storage"]["cached_at"] == first["sample"]["derived_storage"]["cached_at"]
    assert second["sample"]["derived_storage"]["cache_age"] is not None
    assert second["sample"]["derived_storage"]["text_projection_bytes"] == 4096
    assert second["sample"]["derived_storage"]["fts_index_bytes"] == 8192
    assert second["sample"]["derived_storage"]["property_index_bytes"] == 12288


async def test_unsupported_dbstat_before_first_measurement_is_stale_and_unknown():
    async def read(callback, token=None):
        if callback is sample_status:
            return cheap_sample()
        assert callback is sample_derived_storage
        assert isinstance(token, OperationToken)
        return {"available": False, "reason": "DBSTAT_UNAVAILABLE"}

    sampler = StatusSampler(Mock(read=read))
    await sampler.refresh()
    await sampler.refresh_derived()
    snapshot = sampler.snapshot()
    derived = snapshot["sample"]["derived_storage"]
    assert snapshot["available"] is True
    assert snapshot["stale"] is False
    assert derived["available"] is False
    assert derived["stale"] is True
    assert derived["reason"] == "DBSTAT_UNAVAILABLE"
    assert derived["last_error"] == "DBSTAT_UNAVAILABLE"
    assert derived["cached_at"] is None
    assert derived["cache_age"] is None
    assert derived["text_projection_bytes"] is None
    assert derived["fts_index_bytes"] is None
    assert derived["property_index_bytes"] is None


async def test_unsupported_dbstat_after_success_keeps_complete_last_known_measurement():
    derived_calls = 0

    async def read(callback, token=None):
        nonlocal derived_calls
        if callback is sample_status:
            return cheap_sample()
        assert callback is sample_derived_storage
        assert isinstance(token, OperationToken)
        derived_calls += 1
        if derived_calls == 1:
            return {
                "available": True,
                "reason": None,
                "text_projection_bytes": 4096,
                "fts_index_bytes": 8192,
                "property_index_bytes": 12288,
            }
        return {"available": False, "reason": "DBSTAT_UNAVAILABLE"}

    sampler = StatusSampler(Mock(read=read))
    await sampler.refresh()
    await sampler.refresh_derived()
    before = sampler.snapshot()["sample"]["derived_storage"]
    await sampler.refresh_derived()
    snapshot = sampler.snapshot()
    derived = snapshot["sample"]["derived_storage"]
    assert snapshot["available"] is True
    assert snapshot["stale"] is False
    assert derived["available"] is True
    assert derived["stale"] is True
    assert derived["reason"] is None
    assert derived["last_error"] == "DBSTAT_UNAVAILABLE"
    assert derived["cached_at"] == before["cached_at"]
    assert derived["cache_age"] is not None
    assert derived["text_projection_bytes"] == 4096
    assert derived["fts_index_bytes"] == 8192
    assert derived["property_index_bytes"] == 12288
