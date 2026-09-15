"""Pure maintenance recovery boundaries and bounded status diagnostics."""

import time

import pytest

from justpen_knowledgebase_mcp.config import WorkspacePolicy
from justpen_knowledgebase_mcp.storage.maintenance import CheckpointMaintenance, StatusCache, WalState


@pytest.mark.parametrize(
    ("log", "allocated", "restarted", "expected"),
    [
        (65536, 67108864, False, "normal"),
        (65536, 134217728, True, "normal"),
        (65536, 134217728, False, "pressure"),
        (65537, 67108864, True, "pressure"),
        (-1, 67108864, True, "unknown"),
    ],
)
def test_recovery_requires_both_low_backlog_and_reuse(log, allocated, restarted, expected):
    state = CheckpointMaintenance._result(
        WalState(phase="pressure"),
        WorkspacePolicy(),
        log,
        0,
        1024,
        allocated,
        restarted=restarted,
        result="restart" if restarted else "passive",
    )
    assert state.phase == expected


def test_unknown_status_does_not_invent_zero_measurements_or_completion():
    snapshot = StatusCache().snapshot()
    assert snapshot["unavailable"]
    assert snapshot["allocated_bytes"] is None
    assert snapshot["sample_age"] is None
    assert snapshot["estimated_completion_ms"] is None
    assert snapshot["retry_after_ms"] == 1000


def test_status_reports_remaining_shared_cooldown():
    cache = StatusCache()
    cache.update(WalState(next_attempt_not_before=time.time() + 4))
    snapshot = cache.snapshot()
    retry = snapshot["retry_after_ms"]
    assert isinstance(retry, int)
    assert 3900 < retry <= 4000
    assert snapshot["estimated_completion_ms"] is None


def test_incomplete_normal_state_is_reported_unknown():
    cache = StatusCache()
    cache.update(WalState(phase="normal"))
    assert cache.snapshot()["phase"] == "unknown"
