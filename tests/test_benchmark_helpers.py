"""Harness failure accounting and scoped instrumentation remain truthful and reversible."""

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
