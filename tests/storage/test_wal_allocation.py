"""Real containment of the cached WAL measurement between full revalidations."""

import os
from pathlib import Path

import pytest

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.storage import maintenance
from justpen_knowledgebase_mcp.workspace import WorkspacePaths

pytestmark = pytest.mark.integration

BASELINE = 1234


@pytest.fixture
def frozen(monkeypatch):
    """Hold the revalidation clock still so every probe lands in one window."""
    clock = [1000.0]
    monkeypatch.setattr(maintenance.time, "monotonic", lambda: clock[0])
    return clock


@pytest.fixture
def workspace(tmp_path):
    """A live workspace whose WAL exists and measures a known size."""
    root = tmp_path / "workspace"
    root.mkdir()
    with WorkspacePaths(ServerConfig(workspace_dir=root)) as paths:
        wal = Path(str(paths.db) + "-wal")
        wal.write_bytes(b"\0" * BASELINE)
        yield paths, wal


def test_substituted_symlink_is_not_measured_inside_the_cache_window(frozen, workspace, tmp_path):
    """A cached name would stat by name and follow the link out of the workspace."""
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"\0" * 999999)
    paths, wal = workspace
    assert maintenance.allocation(paths) == BASELINE

    wal.unlink()
    wal.symlink_to(outside)

    assert frozen[0] == 1000.0, "the probe must run before the next full revalidation"
    assert maintenance.allocation(paths) is None


def test_hard_linked_wal_is_not_measured_inside_the_cache_window(frozen, workspace):
    """`st_nlink != 1` is `UNSAFE_MANAGED_FILE`, cached path or not."""
    paths, wal = workspace
    assert maintenance.allocation(paths) == BASELINE

    wal.unlink()
    alias = wal.parent / "alias.bin"
    alias.write_bytes(b"\0" * 10)
    os.link(alias, wal)

    assert frozen[0] == 1000.0
    assert maintenance.allocation(paths) is None


def test_replaced_managed_directory_measures_the_pinned_inode_then_degrades(frozen, workspace):
    """Inside the window the pinned descriptor still holds the WAL the database opened."""
    paths, wal = workspace
    assert maintenance.allocation(paths) == BASELINE

    replacement = paths.root / "swapped"
    replacement.mkdir()
    (replacement / wal.name).write_bytes(b"\0" * 77)
    wal.parent.rename(paths.root / "detached")
    replacement.rename(wal.parent)

    assert maintenance.allocation(paths) == BASELINE

    # Past the window the full check runs again and degrades, as it does today.
    frozen[0] += maintenance._REVALIDATE_SECONDS
    assert maintenance.allocation(paths) is None


def test_the_cheap_path_revalidates_at_most_once_per_interval(frozen, workspace, monkeypatch):
    """Reusing the validation is what makes `allocation()` cheap on every read."""
    paths, _wal = workspace
    calls = []
    original = paths.validate_native
    monkeypatch.setattr(
        paths, "validate_native", lambda value: (calls.append(value), original(value))[1], raising=False
    )

    for _ in range(50):
        assert maintenance.allocation(paths) == BASELINE
    assert len(calls) == 1

    frozen[0] += maintenance._REVALIDATE_SECONDS
    assert maintenance.allocation(paths) == BASELINE
    assert len(calls) == 2


def test_a_missing_wal_is_zero_on_both_the_cached_and_the_full_path(frozen, workspace):
    """An absent WAL is a real measurement of zero, not an unknown."""
    paths, wal = workspace
    assert maintenance.allocation(paths) == BASELINE

    wal.unlink()
    assert maintenance.allocation(paths) == 0

    frozen[0] += maintenance._REVALIDATE_SECONDS
    assert maintenance.allocation(paths) == 0
