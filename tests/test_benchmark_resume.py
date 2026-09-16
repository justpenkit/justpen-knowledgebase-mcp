"""Continuation refuses foreign state and keeps prior measurement evidence intact."""

import hashlib
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.service import KnowledgeBase

from .test_runtime_validation import load_script

SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts"

resume = load_script("kb_benchmark_resume")


@pytest.mark.integration
@pytest.mark.parametrize(
    ("field", "value"), [("status", "running"), ("scale", "large"), ("seed", 0), ("provenance_at_start", None)]
)
def test_unknown_or_active_previous_attempt_is_never_resumed(tmp_path, monkeypatch, field, value):
    previous = {
        "status": "partial",
        "scale": "small",
        "seed": 20260916,
        "provenance_at_start": {"python_source_sha256": {"src/core.py": "digest"}},
    }
    previous[field] = value
    original = json.dumps(previous)
    (tmp_path / "report.json").write_text(original)
    monkeypatch.setattr(resume, "core_hashes", lambda: {"src/core.py": "digest"})
    with pytest.raises(RuntimeError):
        resume.previous_attempt(tmp_path, "small", 20260916)
    assert (tmp_path / "report.json").read_text() == original
    assert not list(tmp_path.glob("report-attempt-*.json"))


@pytest.mark.integration
def test_source_mismatch_refuses_and_archive_preserves_exact_bytes(tmp_path, monkeypatch):
    raw = b'{"status":"partial","scale":"small","seed":20260916,"provenance_at_start":{"python_source_sha256":{"src/core.py":"old"}}}\n'
    (tmp_path / "report.json").write_bytes(raw)
    monkeypatch.setattr(resume, "core_hashes", lambda: {"src/core.py": "new"})
    with pytest.raises(RuntimeError, match="source changed"):
        resume.previous_attempt(tmp_path, "small", 20260916)
    monkeypatch.setattr(resume, "core_hashes", lambda: {"src/core.py": "old"})
    prior = resume.previous_attempt(tmp_path, "small", 20260916)
    assert prior["archived_report_sha256"] == hashlib.sha256(raw).hexdigest()
    archive = tmp_path / resume.archive_attempt(tmp_path)
    assert archive.read_bytes() == raw
    assert archive.stat().st_mode & 0o777 == 0o444


@pytest.mark.integration
def test_exclusive_owner_rejects_second_run_then_releases(tmp_path):
    with (
        resume.benchmark_owner(tmp_path),
        pytest.raises(RuntimeError, match="another benchmark"),
        resume.benchmark_owner(tmp_path),
    ):
        pytest.fail("second benchmark entered")
    with resume.benchmark_owner(tmp_path):
        pass


@pytest.mark.parametrize("state", ["queued", "running", "failed"])
async def test_prior_durable_work_settles_without_forcing_state_or_source(state):
    kb = SimpleNamespace(
        workers=SimpleNamespace(read=AsyncMock(return_value=[("job", state, "BUSY")])),
        jobs=AsyncMock(return_value={"state": "queued"}),
        job_runner=SimpleNamespace(wait=AsyncMock(return_value={"state": "completed"})),
    )
    measure = SimpleNamespace(report={}, check=Mock(), deadline=float("inf"))
    await resume.settle_jobs(measure, kb)
    if state == "failed":
        kb.jobs.assert_awaited_once_with({"action": "retry", "job_id": "job"})
    else:
        kb.jobs.assert_not_awaited()
    kb.job_runner.wait.assert_awaited_once()


async def test_foreign_canonical_node_stops_before_any_continuation():
    kb = SimpleNamespace(
        workers=SimpleNamespace(
            read=AsyncMock(return_value=[(1, "node", "endpoint", '{"foreign":true}', "{}", "ready", "wrong-key")])
        )
    )
    measure = SimpleNamespace(nodes=100, hub="", check=Mock())
    with pytest.raises(RuntimeError, match="foreign/mismatched"):
        await resume.validate_nodes(measure, kb, lambda _index: {"expected": True})
    assert kb.workers.read.await_count == 1
    assert measure.hub == ""


@pytest.mark.integration
async def test_real_schema_canonical_node_validation(tmp_path):
    def expected(index):
        return {"url": f"https://bench.example/{index}", "method": "GET"}

    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        await kb.write({"nodes": [{"type": "endpoint", "properties": expected(index)} for index in range(100)]})
        measure = SimpleNamespace(nodes=100, hub="", check=Mock())
        assert await resume.validate_nodes(measure, kb, expected) == 100
        assert measure.hub


@pytest.mark.integration
@pytest.mark.parametrize("corruption", ["foreign_node", "node_key", "relation_key", "inline_reassignment"])
async def test_real_deterministic_corpus_reconciles_relations_blobs_and_links(tmp_path, monkeypatch, corruption):
    monkeypatch.syspath_prepend(str(SCRIPT_ROOT))
    benchmark = load_script("benchmark_knowledgebase")
    measure = benchmark.Measurements(tmp_path, "smoke", 60, "corpus")
    measure.nodes, measure.edges, measure.raw_bytes = 100, 100, 256
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with KnowledgeBase.open(ServerConfig(workspace_dir=workspace)) as kb:
        await measure.graph(kb)
        await measure.evidence(kb)
        measure.report["completed"] = {"nodes": 0, "relations": 0, "raw_text_bytes": 0}
        await resume.reconcile(measure, kb, benchmark.node_properties, benchmark.text_chunk)
        assert measure.report["resumed_confirmed"] == {"nodes": 100, "relations": 100, "raw_text_bytes": 256}
        assert measure.report["resume_evidence_links"] == {"nodes": 1, "foreign": 0, "relations": 0}
        assert measure.inline_present
        original_report = (tmp_path / "report.json").read_bytes()
        changes = {
            "foreign_node": "update node_evidence set node_id=2",
            "node_key": "update nodes set key='corrupted' where id=1",
            "relation_key": "update relations set key='corrupted' where id=1",
            "inline_reassignment": "update node_evidence set evidence_id=(select id from evidence where byte_size=18)",
        }
        await kb.workers.write(lambda c, _t: c.execute(changes[corruption]).fetchall())
        with pytest.raises(RuntimeError, match=r"foreign|mismatched"):
            await resume.reconcile(measure, kb, benchmark.node_properties, benchmark.text_chunk)
        assert (tmp_path / "report.json").read_bytes() == original_report


@pytest.mark.integration
@pytest.mark.parametrize("artifact", ["report.json", "variants"])
def test_fresh_run_preserves_existing_report_and_variant_artifacts(tmp_path, monkeypatch, artifact):
    monkeypatch.syspath_prepend(str(SCRIPT_ROOT))
    benchmark = load_script("benchmark_knowledgebase")
    target = tmp_path / artifact
    if artifact == "variants":
        target.mkdir()
        target = target / "prior.json"
    target.write_bytes(b"immutable prior evidence")
    monkeypatch.setattr(sys, "argv", ["benchmark", "--output", str(tmp_path)])
    with pytest.raises(SystemExit) as failure:
        benchmark.main()
    assert failure.value.code == 2
    assert target.read_bytes() == b"immutable prior evidence"


@pytest.mark.integration
def test_fresh_collision_is_checked_after_ownership_acquisition(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT_ROOT))
    benchmark = load_script("benchmark_knowledgebase")
    original = b"report committed by previous owner"

    @contextmanager
    def previous_owner_finishes(_output):
        (tmp_path / "report.json").write_bytes(original)
        yield

    monkeypatch.setattr(benchmark, "benchmark_owner", previous_owner_finishes)
    monkeypatch.setattr(sys, "argv", ["benchmark", "--output", str(tmp_path)])
    monkeypatch.setattr(benchmark, "execute", lambda *_args: (tmp_path / "report.json").write_bytes(b"overwritten"))
    with pytest.raises(SystemExit) as failure:
        benchmark.main()
    assert failure.value.code == 2
    assert (tmp_path / "report.json").read_bytes() == original


@pytest.mark.integration
@pytest.mark.parametrize("status", ["completed", "partial"])
@pytest.mark.parametrize("mismatch", [None, "source", "counts"])
async def test_variants_accepts_provenanced_complete_corpus_only(tmp_path, monkeypatch, status, mismatch):
    monkeypatch.syspath_prepend(str(SCRIPT_ROOT))
    benchmark = load_script("benchmark_knowledgebase")
    source = tmp_path / "source"
    source.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    measure = benchmark.Measurements(output, "smoke", 60, "variants")
    measure.corpus_output = source
    original = json.dumps(
        {
            "status": status,
            "scale": "smoke",
            "seed": benchmark.SEED,
            "completed": {} if mismatch == "counts" else measure.report["requested"],
            "provenance_at_start": {"python_source_sha256": {"src/core.py": "old"}},
        }
    ).encode()
    (source / "report.json").write_bytes(original)
    monkeypatch.setattr(
        sys.modules["kb_benchmark_resume"],
        "core_hashes",
        lambda: {"src/core.py": "new" if mismatch == "source" else "old"},
    )
    variants = Mock(return_value={"verified": True})
    monkeypatch.setattr(benchmark, "run_variants", variants)
    if mismatch:
        with pytest.raises(RuntimeError, match=r"source changed|incomplete"):
            await measure.run()
        variants.assert_not_called()
    else:
        await measure.run()
        assert measure.report["status"] == "completed"
        assert measure.report["source_report_sha256"] == hashlib.sha256(original).hexdigest()
        assert measure.report["variants"] == {"verified": True}
    assert (source / "report.json").read_bytes() == original
    assert not list(source.glob("report-attempt-*.json"))
    if status == "completed":
        with pytest.raises(RuntimeError, match="source changed" if mismatch == "source" else "partial"):
            benchmark.previous_attempt(source, "smoke", benchmark.SEED)
