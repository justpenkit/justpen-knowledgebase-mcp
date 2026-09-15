"""Runtime-only commands keep locked tools isolated from the developer environment."""

import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from justpen_knowledgebase_mcp.storage.admission import DbAdmissionGate
from justpen_knowledgebase_mcp.storage.connection import ManagedConnection
from justpen_knowledgebase_mcp.storage.evidence import EvidenceStore
from justpen_knowledgebase_mcp.storage.maintenance import CheckpointMaintenance


def load_script(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[1] / "scripts" / (name + ".py")
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime_validation = load_script("runtime_validation")
Instrumentation = load_script("kb_benchmark_instrumentation").Instrumentation


@pytest.mark.parametrize("suite", ["native", "consumer", "benchmark"])
def test_runtime_commands_isolate_sync_and_constrain_tool_closure(tmp_path, monkeypatch, suite):
    (tmp_path / "uv.lock").write_text(
        "\n".join(f'[[package]]\nname="{name}"\nversion="1.2.3"' for name in ["pytest", "pytest-asyncio", "pytest-cov"])
    )
    monkeypatch.setattr(runtime_validation, "ROOT", tmp_path)
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "developer-venv"))
    calls = []
    monkeypatch.setattr(runtime_validation, "run", lambda command, **kwargs: calls.append((command, kwargs)))
    arguments = ["--scale", "smoke", "--output", "with spaces"] if suite == "benchmark" else []
    runtime_validation.execute(suite, arguments)
    sync, settings = calls[0]
    assert sync[:4] == ["uv", "sync", "--locked", "--no-default-groups"]
    env = settings["env"]
    assert "VIRTUAL_ENV" not in env
    assert env["UV_PROJECT_ENVIRONMENT"] != str(tmp_path / "developer-venv")
    assert not Path(env["UV_PROJECT_ENVIRONMENT"]).parent.exists()
    if suite == "benchmark":
        assert len(calls) == 2
        assert calls[-1][0][2:] == [str(tmp_path / "scripts/benchmark_knowledgebase.py"), *arguments]
    else:
        assert len(calls) == 4
        assert calls[1][0][:5] == ["uv", "export", "--locked", "--all-groups", "--no-emit-project"]
        assert calls[2][0][5:7] == ["--constraint", calls[1][0][-1]]
        assert calls[2][0][7:] == ["pytest==1.2.3", "pytest-asyncio==1.2.3", "pytest-cov==1.2.3"]
        assert calls[-1][0][1:] == ["-B", "-m", "pytest", runtime_validation.SUITES[suite], "-v"]


def test_instrumentation_restores_all_class_methods_on_failure():
    gate_methods = dict(vars(DbAdmissionGate))
    connection_methods = dict(vars(ManagedConnection))
    evidence_methods = dict(vars(EvidenceStore))
    maintenance_methods = dict(vars(CheckpointMaintenance))
    checkpoint = ManagedConnection.wal_checkpoint
    measurements = Instrumentation()

    def fail():
        with measurements.active():
            assert DbAdmissionGate.transaction is not gate_methods["transaction"]
            assert ManagedConnection.wal_checkpoint is not checkpoint
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        fail()
    assert dict(vars(DbAdmissionGate)) == gate_methods
    assert dict(vars(ManagedConnection)) == connection_methods
    assert dict(vars(EvidenceStore)) == evidence_methods
    assert dict(vars(CheckpointMaintenance)) == maintenance_methods


def test_instrumentation_bounds_retention_and_labels_missing_denominator():
    measurements = Instrumentation()
    measurements.samples["example"].extend(range(10005))
    measurements.counts["example"] = 10005
    result = measurements.report(0)
    assert result["latency_ms"]["example"]["total_count"] == 10005
    assert result["latency_ms"]["example"]["retained_count"] == 10000
    assert result["pressure_episodes_per_ingested_gib"] is None
    assert any("recent10000" in limit for limit in result["limits"])


def test_instrumentation_concurrent_counts_have_no_lost_updates():
    measurements = Instrumentation()

    def writer(offset):
        for index in range(5000):
            measurements.record("parallel", float(index))
            measurements.increment("events")
            measurements.maximum("peak", offset + index)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(writer, range(0, 40000, 5000)))
    report = measurements.report(0)
    assert report["counts"]["parallel"] == report["counts"]["events"] == 40000
    assert report["latency_ms"]["parallel"]["retained_count"] == 10000
    assert report["high_water"]["peak"] == 39999


def test_unsampled_pressure_is_unknown_and_attempt_denominator_is_explicit():
    measurements = Instrumentation()
    assert measurements.report(1073741824)["pressure_episodes"] is None
    assert measurements.report(1073741824)["pressure_episodes_per_ingested_gib"] is None
    measurements.status_samples = 1
    measurements.pressure_episodes = 2
    result = measurements.report(536870912)
    assert result["pressure_episodes_per_ingested_gib"] == 4
    assert result["logical_raw_bytes_ingested_this_attempt"] == 536870912
