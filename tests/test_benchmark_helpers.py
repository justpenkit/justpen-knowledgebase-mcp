"""Harness failure accounting and scoped instrumentation remain truthful and reversible."""

import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import apsw
import pytest

from justpen_knowledgebase_mcp.errors import BusyError, LimitError
from justpen_knowledgebase_mcp.storage.jobs import JobStore

from .test_runtime_validation import load_script

lifecycle = load_script("kb_benchmark_lifecycle")


@pytest.mark.parametrize(("prefix", "error"), [("BUSY", BusyError), ("LIMIT", LimitError)])
async def test_wire_error_is_not_counted_as_successful_benchmark_call(prefix, error):
    peer = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=SimpleNamespace(structured_content={"status": "error", "error": prefix + ": fixture"})
        )
    )
    with pytest.raises(error):
        await lifecycle.response_data(peer, "kb_write", {})
    peer.call_tool.assert_awaited_once_with("kb_write", {}, raise_on_error=False)


async def test_successful_wire_data_and_missing_or_unexpected_envelopes():
    peer = SimpleNamespace(
        call_tool=AsyncMock(return_value=SimpleNamespace(structured_content={"status": "ok", "data": {"answer": 42}}))
    )
    assert await lifecycle.response_data(peer, "kb_get", {}) == {"answer": 42}
    for value in [None, {"status": "error", "error": "IO_ERROR: failure"}]:
        peer.call_tool.return_value.structured_content = value
        with pytest.raises(RuntimeError):
            await lifecycle.response_data(peer, "kb_get", {})


async def test_commit_accounting_separates_empty_claims_failures_and_restores(monkeypatch):
    async def original(callback, token=None):
        return callback(None, token)

    workers = SimpleNamespace(write=original, control=original)
    report = {}
    claim = Mock(return_value="claim-token")
    monkeypatch.setattr(JobStore, "claim", claim)

    async def fail():
        with lifecycle.committed_calls(SimpleNamespace(workers=workers), report):
            assert await workers.control(lambda c, _t: JobStore.claim(c, "short", "delete")) == "claim-token"
            claim.return_value = None
            assert await workers.control(lambda c, _t: JobStore.claim(c, "short", "delete")) is None
            claim.side_effect = OSError("commit failed")
            with pytest.raises(OSError):
                await workers.control(lambda c, _t: JobStore.claim(c, "short", "delete"))
            raise RuntimeError("exit failure")

    with pytest.raises(RuntimeError, match="exit failure"):
        await fail()
    assert workers.write is original
    assert workers.control is original
    assert report["committed_calls"] == {"claim": 1, "empty_claim_poll": 1}
    assert report["failed_calls"] == {"claim": 1}


@pytest.mark.integration
def test_offline_audit_deadline_refuses_before_opening_destination(tmp_path):
    variants = load_script("kb_benchmark_variants")
    target = tmp_path / "never-created.sqlite3"
    with pytest.raises(TimeoutError, match="deadline"), variants.audit_connection(target, 0):
        pytest.fail("expired audit opened connection")
    assert not target.exists()


@pytest.mark.integration
def test_offline_audit_connection_closes_on_failure(tmp_path):
    variants = load_script("kb_benchmark_variants")
    connection = None

    def fail():
        nonlocal connection
        with variants.audit_connection(tmp_path / "audit.sqlite3", float("inf")) as connection:
            connection.execute("create table proof(value)")
            raise RuntimeError("fixture")

    with pytest.raises(RuntimeError, match="fixture"):
        fail()
    assert connection is not None
    with pytest.raises(apsw.ConnectionClosedError):
        connection.execute("select 1")


@pytest.mark.integration
async def test_partial_hub_keeps_other_phase_results_but_never_reports_completion(tmp_path, monkeypatch):
    measure = SimpleNamespace(output=tmp_path, report={}, check=Mock(), save=Mock())
    phases = []

    async def phase(_measure, root):
        phases.append(root.name)
        if root.name == "hub":
            measure.report["lifecycle"] = {"hub": {"status": "running"}}

    for name in ["clients", "hub", "recovery", "contention", "copy_takeover"]:
        monkeypatch.setattr(lifecycle, name, phase)
    with pytest.raises(TimeoutError, match="hub cleanup"):
        await lifecycle.run_lifecycle(measure)
    assert phases == ["clients", "hub", "recovery", "contention", "copy_takeover"]
    assert measure.report["lifecycle"]["hub"]["status"] == "running"


@pytest.mark.integration
def test_low_trigger_checkpoint_worker_has_separate_owner_and_bounded_identical_corpus(tmp_path):
    variants = load_script("kb_benchmark_variants")
    results = variants.checkpoint_worker_comparison(
        tmp_path, time.monotonic() + 30, raw_bytes=4194304, low_bytes=1048576
    )
    automatic, worker = results
    for result in results:
        assert result["logical_raw_bytes"] == 4194304
        assert result["confirmed_rows"] == 256
        assert result["synchronous"] == "FULL"
        assert result["transaction_rows"] == 16
        assert result["integrity"] == "ok"
    assert automatic["corpus_sha256"] == worker["corpus_sha256"]
    assert automatic["native_auto_checkpoint_wall_ms"] is None
    assert automatic["native_auto_checkpoint_thread_cpu_ms"] is None
    assert worker["triggered_checkpoints"]
    assert any(
        item["log_backfilled_frames"] and item["log_backfilled_frames"][1] > 0
        for item in worker["triggered_checkpoints"]
    )
    assert worker["max_observed_wal_frames"] * worker["page_size"] >= 1048576
    assert all(item["worker_thread"] != worker["foreground_thread"] for item in worker["triggered_checkpoints"])
    assert all(item["trigger_frames"] * worker["page_size"] >= 1048576 for item in worker["triggered_checkpoints"])
    assert all(item["wall_ms"] >= 0 and item["thread_cpu_ms"] >= 0 for item in worker["triggered_checkpoints"])


@pytest.mark.integration
def test_checkpoint_worker_failure_closes_connections_on_owner_threads(tmp_path, monkeypatch):
    variants = load_script("kb_benchmark_variants")
    original = variants.audit_connection
    ownership = []

    @contextmanager
    def tracked(*args, **kwargs):
        owner = threading.get_ident()
        with original(*args, **kwargs) as connection:
            try:
                yield connection
            finally:
                ownership.append((owner, threading.get_ident()))

    def fail(*_args):
        raise RuntimeError("worker failure")

    monkeypatch.setattr(variants, "audit_connection", tracked)
    monkeypatch.setattr(variants, "worker_checkpoint", fail)
    with pytest.raises(RuntimeError, match="worker failure"):
        variants.checkpoint_worker_variant(tmp_path, time.monotonic() + 30, 0, 4194304, 1048576)
    assert len(ownership) == 2
    assert len({owner for owner, _closed in ownership}) == 2
    assert all(owner == closed for owner, closed in ownership)


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["pragma", "hook"])
def test_checkpoint_setup_failure_closes_maintenance_on_owner(tmp_path, monkeypatch, failure):
    variants = load_script("kb_benchmark_variants")
    original = variants.audit_connection
    ownership = []
    foreground = threading.get_ident()

    class ConnectionProxy:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def pragma(self, *args):
            if failure == "pragma" and threading.get_ident() != foreground:
                raise RuntimeError("setup pragma")
            return self.connection.pragma(*args)

        def set_wal_hook(self, *_args):
            raise RuntimeError("setup hook")

    @contextmanager
    def tracked(*args, **kwargs):
        opener = threading.get_ident()
        with original(*args, **kwargs) as connection:
            try:
                yield ConnectionProxy(connection)
            finally:
                ownership.append((opener, threading.get_ident()))

    monkeypatch.setattr(variants, "audit_connection", tracked)
    with pytest.raises(RuntimeError, match="setup " + failure):
        variants.checkpoint_worker_variant(tmp_path, time.monotonic() + 30, 0, 262144, 65536)
    assert len(ownership) == 2
    assert len({opener for opener, _ in ownership}) == 2
    assert all(opener == closer for opener, closer in ownership)
