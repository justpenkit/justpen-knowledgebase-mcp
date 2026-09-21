"""Harness failure accounting and scoped instrumentation remain truthful and reversible."""

import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import apsw
import pytest

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import BusyError, LimitError
from justpen_knowledgebase_mcp.jobs import JobRunner
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.jobs import JobStore

from .test_runtime_validation import load_script

SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts"

lanes = load_script("kb_benchmark_lanes")
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


async def test_lane_counter_separates_lanes_and_restores_every_worker_entry_point():
    async def original(callback, token=None):
        return callback("connection", token)

    workers = SimpleNamespace(read=original, write=original, control=original)
    with lanes.lane_transactions(workers) as counts:
        for _ in range(3):
            await workers.control(lambda c, _t: c)
        await workers.read(lambda c, _t: c)
        assert await workers.write(lambda c, _t: c) == "connection"
    assert dict(counts) == {"control": 3, "read": 1, "write": 1}
    assert (workers.read, workers.write, workers.control) == (original, original, original)


@pytest.mark.parametrize("terminal", ["finish", "finish_failure"])
async def test_wait_observation_pairs_a_terminal_transition_and_never_invents_a_lag(monkeypatch, terminal):
    stubs = {name: Mock() for name in ["finish", "finish_failure"]}
    for name, stub in stubs.items():
        monkeypatch.setattr(JobStore, name, stub)

    async def wait(_runner, job_id, _deadline, _accepted=None):
        return {"job_id": job_id, "state": "completed"}

    monkeypatch.setattr(JobRunner, "wait", wait)
    runner = cast("JobRunner", SimpleNamespace())
    with lanes.wait_observation() as observations:
        getattr(JobStore, terminal)("connection", SimpleNamespace(job_id="paired"), "completed", {})
        assert (await JobRunner.wait(runner, "paired", 0.0))["state"] == "completed"
        await JobRunner.wait(runner, "never-observed", 0.0)
    stubs[terminal].assert_called_once()
    assert JobStore.finish is stubs["finish"]
    assert JobRunner.wait is wait
    assert observations[0]["observation_lag_ms"] is not None
    assert observations[0]["observation_lag_ms"] >= 0
    assert observations[1]["observation_lag_ms"] is None
    assert all(item["wait_ms"] >= 0 for item in observations)


@pytest.mark.integration
async def test_idle_control_rate_and_job_wait_lag_measure_a_real_idle_server(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        deadline = time.monotonic() + 120
        idle = await lanes.idle_control_rate(kb, deadline, seconds=1.0, settle=0.5)
        waits = await lanes.job_wait_samples(kb, deadline, samples=2)
    assert idle["idle"]
    assert idle["jobs_by_state_before"] == idle["jobs_by_state_after"] == {}
    assert idle["transactions"]["control"] > 0
    assert idle["transactions_per_second"]["control"] == idle["transactions"]["control"] / idle["window_seconds"]
    assert waits["observed_waits"] == 2
    assert waits["unpaired_waits"] == 0
    assert len(waits["observation_lag_ms_samples"]) == 2
    assert all(value >= 0 for value in waits["observation_lag_ms_samples"])


@pytest.mark.integration
async def test_traversal_scenario_measures_both_documented_edge_budgets(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT_ROOT))
    benchmark = load_script("benchmark_knowledgebase")
    measure = benchmark.Measurements(tmp_path, "smoke", 60, "corpus")
    measure.hub = "hub-id"
    requests = []
    kb = SimpleNamespace(
        search=AsyncMock(return_value={"canonical_scan_count": 0}),
        neighbors=AsyncMock(side_effect=lambda request: requests.append(request) or {}),
        status=AsyncMock(return_value={}),
        workers=SimpleNamespace(read=AsyncMock(return_value={})),
    )
    await measure.queries(kb, "warm")
    assert [request["max_edges"] for request in requests] == [100] * 10 + [300] * 10
    assert {request["max_nodes"] for request in requests} == {100}
    assert measure.report["corpus"]["traversal_max_edges"] == [100, 300]
    assert len(measure.latencies["warm_traversal_max_edges_100"]) == 10
    assert len(measure.latencies["warm_traversal_max_edges_300"]) == 10


@pytest.mark.integration
async def test_lane_phase_summarizes_raw_samples_through_the_shared_quantile_authority(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT_ROOT))
    benchmark = load_script("benchmark_knowledgebase")
    measure = benchmark.Measurements(tmp_path, "smoke", 60, "lanes")

    async def measured(_measure, root):
        assert root == tmp_path / "lanes"
        return {
            "idle_control_transactions": {"transactions_per_second": {"control": 49.0}},
            "job_wait": {"observation_lag_ms_samples": [3.0, 1.0, 2.0], "wait_ms_samples": [6.0, 4.0, 5.0]},
        }

    monkeypatch.setattr(benchmark, "run_lanes", measured)
    await measure.run()
    waits = measure.report["lanes"]["job_wait"]
    assert measure.report["status"] == "completed"
    assert measure.report["lanes"]["idle_control_transactions"]["transactions_per_second"]["control"] == 49.0
    assert waits["observation_lag_ms"] == {"count": 3, "p50": 2.0, "p95": 2.0, "p99": 2.0}
    assert waits["wait_ms"] == {"count": 3, "p50": 5.0, "p95": 5.0, "p99": 5.0}
    assert "observation_lag_ms_samples" not in waits
    assert "wait_ms_samples" not in waits
