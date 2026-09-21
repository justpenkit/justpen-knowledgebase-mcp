"""Bounded, reproducible graph/evidence measurements, never a product policy override."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import apsw
from kb_benchmark_instrumentation import Instrumentation
from kb_benchmark_lanes import run_lanes
from kb_benchmark_lifecycle import run_lifecycle
from kb_benchmark_resume import archive_attempt, benchmark_owner, previous_attempt, reconcile, source_attempt
from kb_benchmark_variants import checkpoint_worker_comparison, run_variants

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import BusyError, LimitError, McpError
from justpen_knowledgebase_mcp.service import KnowledgeBase

if TYPE_CHECKING:
    from collections.abc import Awaitable

SCALES = {
    "smoke": (200, 1000, 1048576),
    "small": (100000, 1000000, 1073741824),
    "medium": (1000000, 5000000, 10737418240),
    "large": (1000000, 5000000, 53687091200),
}
SEED = 20260916
TRAVERSAL_MAX_EDGES = [100, 300]
Result = TypeVar("Result")


REDIRECT_STATUSES = (301, 302, 303, 307, 308)


def edge_properties(ordinal: int) -> dict[str, Any]:
    """One canonical `redirects_to` property authority shared by generation and resume verification."""
    return {"status": REDIRECT_STATUSES[ordinal % len(REDIRECT_STATUSES)], "context": f"seed{SEED}-edge{ordinal}"}


def edge_target_rank(ordinal: int) -> int:
    """One canonical target authority: `redirects_to` identity is one edge per target and status.

    `catalog.py` gives `redirects_to` `identity=_identity(["status"])`, so a hub carries exactly
    `len(REDIRECT_STATUSES)` distinct edges to any one target. Ordinals therefore walk the whole
    (target, status) product instead of cycling targets, which would restate an identity already used.
    Rank r is node id r+1, so rank 0 is the hub's permitted self-edge (`self_edge=True`).
    """
    return ordinal // len(REDIRECT_STATUSES)


def node_properties(index: int) -> dict[str, Any]:
    """One canonical node authority shared by generation and resume verification."""
    properties: dict[str, Any] = {
        "url": f"https://bench.example/{index}",
        "method": "GET",
        "status": 403 if index % 97 == 0 else 200,
    }
    if index % 100 == 0:
        properties.update(array=list(range(600)), value1024="x" * 1024, value1025="x" * 1025)
        properties["p" * 1025] = index
    return properties


def text_chunk(index: int, size: int) -> bytes:
    """One bounded deterministic raw chunk authority, including rare-token frequency."""
    header = f"document{index} seed{SEED} " + ("raretoken " if index % 97 == 0 else "ordinary ")
    line = (header + "alpha beta exactliteral proof\n").encode()
    return (line * (size // len(line) + 1))[:size]


def distribution(values: list[float]) -> dict[str, float | int | None]:
    """Keep missing observations distinct from measured zero latency."""
    ordered = sorted(values)
    return {
        "count": len(values),
        **{
            name: ordered[min(len(ordered) - 1, int((len(ordered) - 1) * quantile))] if ordered else None
            for name, quantile in [("p50", 0.50), ("p95", 0.95), ("p99", 0.99)]
        },
    }


def disk_sizes(root: Path) -> dict[str, int]:
    """Measure logical file lengths separately from decoded SQL/FTS content."""
    result = {"database": 0, "wal": 0, "evidence": 0, "other": 0}
    for path in root.rglob("*"):
        if path.is_file():
            category = (
                "database"
                if path.name == "graph.sqlite3"
                else "wal"
                if path.name.endswith("-wal")
                else "evidence"
                if "evidence" in path.parts
                else "other"
            )
            result[category] += path.stat().st_size
    return result


class Measurements:
    """One bounded run; report partial progress if the explicit time budget expires."""

    def __init__(self, output: Path, scale: str, seconds: float, phase: str = "all") -> None:
        """Initialize bounded measurement and corpus metadata."""
        self.instrumentation = Instrumentation()
        self.output, self.scale = output, scale
        self.phase = phase
        self.resume = False
        self.corpus_output: Path | None = None
        self.inline_present = False
        self.nodes, self.edges, self.raw_bytes = SCALES[scale]
        self.deadline = time.monotonic() + seconds
        self.report: dict[str, Any] = {
            "scale": scale,
            "phase": phase,
            "provenance_at_start": source_provenance(),
            "status": "running",
            "seed": SEED,
            "requested": {"nodes": self.nodes, "relations": self.edges, "raw_text_bytes": self.raw_bytes},
            "completed": {"nodes": 0, "relations": 0, "raw_text_bytes": 0},
            "runtime": {
                "python": sys.version,
                "platform": platform.platform(),
                "machine": platform.machine(),
                "cpu_count": os.cpu_count(),
                "sqlite": apsw.sqlitelibversion(),
                "apsw": apsw.apswversion(),
            },
            "corpus": {
                "asset_type_count": 1,
                "asset_types": ["endpoint"],
                "relation_type": "redirects_to",
                "relation_identity": "one edge per (target, status); the hub's own self-edge is rank 0",
                "relation_ceiling": self.nodes * len(REDIRECT_STATUSES),
                "batch_records": 100,
                "hub": "first endpoint",
                "paths": ["/url", "/method", "/status", "/array", "/value1024", "/value1025"],
                "large_array_every": 100,
                "long_path_every": 100,
                "lexical_rare_token_every_evidence": 97,
                "evidence_links_per_blob": 1,
                "text_chunk_bytes": 1048576,
                "traversal_max_edges": TRAVERSAL_MAX_EDGES,
            },
            "claim_boundaries": [
                "cold means reopened connections, not flushed operating-system caches",
                "FULL product durability; no normal-mode product change",
                "50GiB raw decoded text can make graph.sqlite3 exceed50GiB",
                "WAL thresholds are admission policy, not disk quotas",
                "No hard50ms/500ms native checkpoint or10-15s query SLA",
                "quantiles use lower empirical order statistic floor((n-1)*q); ten query samples do not establish stable tail latency",
            ],
        }
        self.hub = ""
        self.latencies: dict[str, list[float]] = {}
        self.failed_latencies: dict[str, list[float]] = {}
        self.errors: dict[str, int] = {}

    def check(self) -> None:
        """Stop on time or safety-space limits before another bounded batch."""
        if time.monotonic() >= self.deadline:
            raise TimeoutError("explicit benchmark time budget reached")
        free = shutil.disk_usage(self.output).free
        if free < 4 * 1073741824:
            raise OSError("benchmark safety reserve reached; stopped before filling disk")

    def save(self) -> None:
        """Persist measured progress without inventing missing observations."""
        self.report["latency_ms"] = {key: distribution(values) for key, values in self.latencies.items()}
        self.report["failed_attempt_latency_ms"] = {
            key: distribution(values) for key, values in self.failed_latencies.items()
        }
        self.report["latency_population"] = (
            "latency_ms contains successful calls only; failed attempts are separate; no quantiles for unrun calls"
        )
        self.report["errors"] = self.errors
        self.report["disk_bytes"] = disk_sizes(self.output / "workspace")
        self.report["lifecycle_disk_bytes"] = {
            name: disk_sizes(self.output / name)
            for name in ["clients", "hub", "recovery", "contention", "copy_takeover"]
            if (self.output / name).exists()
        }
        self.report["max_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
            1 if sys.platform == "darwin" else 1024
        )
        (self.output / "report.json").write_text(json.dumps(self.report, indent=2) + "\n")

    async def call(self, name: str, operation: Awaitable[Result]) -> Result:
        """Keep successful response latency separate from failed/uncertain attempts."""
        start = time.perf_counter()
        try:
            result = await operation
        except BaseException:
            self.failed_latencies.setdefault(name, []).append((time.perf_counter() - start) * 1000)
            raise
        self.latencies.setdefault(name, []).append((time.perf_counter() - start) * 1000)
        return result

    async def write_batch(self, kb: KnowledgeBase, payload: dict[str, Any], name: str) -> dict[str, Any]:
        """Retry an unchanged idempotent graph payload within a bounded total budget."""
        retry_deadline = min(self.deadline, time.monotonic() + 90)
        while True:
            self.check()
            try:
                return await self.call(name, kb.write(payload))
            except (BusyError, LimitError) as error:
                self.errors[error.error_type] = self.errors.get(error.error_type, 0) + 1
                if time.monotonic() >= retry_deadline:
                    raise
                delay = min(max(1.0, getattr(error, "retry_after_ms", 1000) / 1000), retry_deadline - time.monotonic())
                before = time.perf_counter()
                await asyncio.sleep(max(0, delay))
                self.latencies.setdefault("graph_retry_wait", []).append((time.perf_counter() - before) * 1000)
                self.report["last_retry_status"] = await kb.status()

    def relation_ceiling(self) -> int:
        """State how many distinct hub relations this node count can actually represent."""
        return self.nodes * len(REDIRECT_STATUSES)

    async def graph(self, kb: KnowledgeBase) -> None:
        """Generate strict canonical graph batches through product mutations."""
        if self.edges > self.relation_ceiling():
            raise ValueError(
                f"scale {self.scale} requests {self.edges} hub relations, but {self.nodes} nodes admit at most "
                f"{self.relation_ceiling()}: `redirects_to` identity is one edge per target and status"
            )
        begin = time.perf_counter()
        prior_nodes = self.report["completed"]["nodes"]
        prior_edges = self.report["completed"]["relations"]
        for first in range(prior_nodes, self.nodes, 100):
            self.check()
            batch = [
                {"type": "endpoint", "properties": node_properties(index)}
                for index in range(first, min(first + 100, self.nodes))
            ]
            result = await self.write_batch(kb, {"nodes": batch}, "node_batch_attempt")
            if not self.hub:
                self.hub = result["nodes"][0]["id"]
            self.report["completed"]["nodes"] += len(batch)
        self.report["node_records_per_second"] = (
            (self.nodes - prior_nodes) / (time.perf_counter() - begin) if self.nodes > prior_nodes else None
        )
        begin = time.perf_counter()
        for first in range(prior_edges, self.edges, 100):
            self.check()
            # Fetch a bounded ID page; the entire graph/ID working set is never retained in RAM.
            base = edge_target_rank(first)
            size = min(100, self.edges - first)
            targets = await kb.workers.read(
                lambda c, _t, base=base: c.execute(
                    "select uuid from nodes where id>? order by id limit 100", (base,)
                ).fetchall()
            )
            if len(targets) <= edge_target_rank(first + size - 1) - base:
                raise AssertionError("bounded target page is shorter than its batch of distinct identities needs")
            batch = [
                {
                    "type": "redirects_to",
                    "source_ref": {"id": self.hub},
                    "target_ref": {"id": targets[edge_target_rank(first + index) - base][0]},
                    "properties": edge_properties(first + index),
                }
                for index in range(size)
            ]
            await self.write_batch(kb, {"relations": batch}, "relation_batch_attempt")
            self.report["completed"]["relations"] += len(batch)
            if first % 10000 == 0:
                self.save()
        self.report["relation_records_per_second"] = (
            (self.edges - prior_edges) / (time.perf_counter() - begin) if self.edges > prior_edges else None
        )

    async def evidence(self, kb: KnowledgeBase) -> None:
        """Copy and index one bounded raw evidence file at a time."""
        start = time.perf_counter()
        source = self.output / "workspace/input.txt"
        prior_bytes = self.report["completed"]["raw_text_bytes"]
        for offset in range(prior_bytes, self.raw_bytes, 1048576):
            self.check()
            index = offset // 1048576
            size = min(1048576, self.raw_bytes - offset)
            source.write_bytes(text_chunk(index, size))
            job = await self.call(
                "path_admission",
                kb.ingest_evidence(
                    {"path": "input.txt", "media_type": "text/plain", "targets": [{"kind": "nodes", "id": self.hub}]}
                ),
            )
            job = await self.call("path_ready", kb.job_runner.wait(job["job_id"], self.deadline))
            if job["state"] != "completed":
                raise RuntimeError(f"ingest did not complete: {job}")
            self.report["completed"]["raw_text_bytes"] += size
            if index % 10 == 0:
                self.save()
        self.report["import_mib_per_second"] = (
            (self.raw_bytes - prior_bytes) / 1048576 / (time.perf_counter() - start)
            if self.raw_bytes > prior_bytes
            else None
        )
        source.unlink(missing_ok=True)
        if not self.inline_present:
            await self.call("inline_ready", kb.ingest_evidence({"text": "small inline proof"}))

    async def queries(self, kb: KnowledgeBase, temperature: str) -> None:
        """Measure exact/indexed/fallback and lexical query modes independently."""
        queries = {
            "typed_scalar": {"kind": "nodes", "properties": {"path": "/status", "op": "eq", "value": 403}},
            "unknown_array": {"kind": "nodes", "properties": {"path": "/array/599", "op": "eq", "value": 599}},
            "value1024": {"kind": "nodes", "properties": {"path": "/value1024", "op": "eq", "value": "x" * 1024}},
            "value1025_sentinel": {
                "kind": "nodes",
                "properties": {"path": "/value1025", "op": "eq", "value": "x" * 1025},
            },
            "long_path_omission": {"kind": "nodes", "properties": {"path": "/" + "p" * 1025, "op": "eq", "value": 0}},
            "literal": {"kind": "evidence", "query": "exactliteral", "query_mode": "literal"},
            "words": {"kind": "evidence", "query": "raretoken alpha", "query_mode": "words"},
        }
        for name, request in queries.items():
            scans: list[int | None] = []
            for _ in range(10):
                self.check()
                try:
                    result = await self.call(f"{temperature}_{name}", kb.search(request))
                    scans.append(result.get("canonical_scan_count"))
                except McpError as error:
                    self.errors[error.error_type] = self.errors.get(error.error_type, 0) + 1
            self.report[f"{temperature}_{name}_canonical_scans"] = scans
            self.save()
        for max_edges in TRAVERSAL_MAX_EDGES:
            for _ in range(10):
                self.check()
                await self.call(
                    f"{temperature}_traversal_max_edges_{max_edges}",
                    kb.neighbors({"seed_ids": [self.hub], "max_nodes": 100, "max_edges": max_edges}),
                )
        self.report[temperature + "_status"] = await kb.status()
        self.report[temperature + "_sql_disk"] = await kb.workers.read(
            lambda c, _t: {
                "decoded_text_bytes": c.execute(
                    "select coalesce(sum(length(cast(text as blob))),0) from search_documents"
                ).get,
                "page_size": c.pragma("page_size"),
                "page_count": c.pragma("page_count"),
                "freelist_count": c.pragma("freelist_count"),
                "tables": c.execute("select name,sum(pgsize) from dbstat group by name").fetchall(),
            }
        )

    async def lanes(self) -> None:
        """Measure idle control-lane pressure and job-wait observation lag on an empty workspace."""
        result = await run_lanes(self, self.output / "lanes")
        waits = result["job_wait"]
        waits["observation_lag_ms"] = distribution(waits.pop("observation_lag_ms_samples"))
        waits["wait_ms"] = distribution(waits.pop("wait_ms_samples"))
        self.report["lanes"] = result
        self.save()

    async def run(self) -> None:
        """Measure product lifecycle before isolated comparison experiments."""
        if self.phase == "lanes":
            await self.lanes()
            self.report["status"] = "completed"
            return
        if self.phase == "checkpoint":
            self.report["checkpoint_worker_comparison"] = await asyncio.to_thread(
                checkpoint_worker_comparison, self.output / "checkpoint", self.deadline
            )
            self.report["status"] = "completed"
            return
        if self.phase == "variants":
            if self.corpus_output is None:
                raise RuntimeError("variant phase requires a closed corpus output")
            previous = source_attempt(self.corpus_output, self.scale, SEED)
            if previous["completed"] != self.report["requested"]:
                raise RuntimeError("variant source corpus is incomplete")
            self.report["source_report_sha256"] = previous["archived_report_sha256"]
            self.report["source_corpus_output"] = str(self.corpus_output)
            self.report["variants"] = await asyncio.to_thread(
                run_variants,
                self.output / "variants",
                self.corpus_output / "workspace/.justpen/knowledgebase/graph.sqlite3",
                self.deadline,
            )
            self.report["status"] = "completed"
            return
        if self.phase == "lifecycle":
            await run_lifecycle(self)
            self.report["status"] = "completed"
            return
        root = self.output / "workspace"
        root.mkdir(exist_ok=self.resume)
        config = ServerConfig(workspace_dir=root)
        async with KnowledgeBase.open(config) as kb:
            sampler = asyncio.create_task(self.instrumentation.sample(kb))
            try:
                if self.resume:
                    await reconcile(self, kb, node_properties, text_chunk)
                await self.graph(kb)
                await self.evidence(kb)
                await self.queries(kb, "warm")
            finally:
                try:
                    self.report["persisted_counts"] = await kb.workers.read(
                        lambda c, _t: {
                            "nodes": c.execute("select count(*) from nodes").get,
                            "relations": c.execute("select count(*) from relations").get,
                        }
                    )
                except (McpError, OSError) as error:
                    self.report["persisted_counts"] = {"unavailable": str(error)}
                finally:
                    self.instrumentation.stopped.set()
                    await sampler
            closing = time.perf_counter()
        self.report["last_close_ms"] = (time.perf_counter() - closing) * 1000
        self.report["shutdown_exceeded_30s_grace"] = self.report["last_close_ms"] > 30000
        async with KnowledgeBase.open(config) as kb:
            await self.queries(kb, "reopened")
        self.report["variants"] = await asyncio.to_thread(
            run_variants, self.output / "variants", root / ".justpen/knowledgebase/graph.sqlite3", self.deadline
        )
        if self.phase == "all":
            await self.lanes()
            await run_lifecycle(self)
        self.report["status"] = "completed"


def source_provenance() -> dict[str, Any]:
    """Capture loaded-run source identity before mutation or benchmark work starts."""
    repository = Path(__file__).resolve().parents[1]
    hashes = {
        str(path.relative_to(repository)): hashlib.sha256(path.read_bytes()).hexdigest()
        for directory in ["src", "scripts"]
        for path in sorted((repository / directory).rglob("*.py"))
    }
    return {
        "pid": os.getpid(),
        "captured_at_unix": time.time(),
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip(),
        "dirty_status": subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=repository, text=True
        ).splitlines(),
        "python_source_sha256": hashes,
    }


def main() -> None:
    """Refuse oversized reservations before generating files; keep partial evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", choices=SCALES, default="smoke")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-seconds", type=float, default=600)
    parser.add_argument(
        "--phase", choices=["corpus", "lifecycle", "lanes", "variants", "checkpoint", "all"], default="all"
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--corpus-output", type=Path)
    options = parser.parse_args()
    output = options.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if options.resume and options.phase != "corpus":
        parser.error("resume supports only --phase corpus")
    if (options.phase == "variants") != (options.corpus_output is not None):
        parser.error("--corpus-output is required only for --phase variants")
    with benchmark_owner(output):
        if not options.resume and any(
            (output / name).exists()
            for name in [
                "report.json",
                "variants",
                "workspace",
                "lanes",
                "clients",
                "hub",
                "recovery",
                "contention",
                "copy_takeover",
            ]
        ):
            parser.error("output artifacts exist; choose a new output directory")
        if options.corpus_output is not None:
            with benchmark_owner(options.corpus_output.resolve()):
                execute(options, output)
        else:
            execute(options, output)


def execute(options: argparse.Namespace, output: Path) -> None:
    """Own one run/continuation, preserving previous attempt evidence before writes."""
    measure = Measurements(output, options.scale, options.max_seconds, options.phase)
    measure.corpus_output = options.corpus_output.resolve() if options.corpus_output is not None else None
    if options.resume:
        previous = previous_attempt(output, options.scale, SEED)
        archive = archive_attempt(output)
        measure.resume = True
        measure.report["resume_parent"] = {
            "artifact": archive,
            "sha256": previous["archived_report_sha256"],
            "previous_completed": previous["completed"],
            "legacy_provenance_limitation": previous.get("legacy_provenance_limitation"),
        }
    nodes, edges, text = SCALES[options.scale]
    reservation = text * 8 + nodes * 8192 + edges * 4096 + 4 * 1073741824
    if options.phase == "lifecycle":
        reservation = 8 * 1073741824
    elif options.phase == "lanes":
        reservation = 4 * 1073741824
    elif options.phase == "all":
        reservation += 4 * 1073741824
    measure.report["resource_preflight"] = {
        "available_bytes": shutil.disk_usage(output).free,
        "required_budget_bytes": reservation,
        "estimate_not_quota": True,
    }
    if shutil.disk_usage(output).free < reservation:
        measure.report.update(
            status="not_run", reason="insufficient conservative disk budget including backup/compaction"
        )
        measure.save()
        raise SystemExit(2)
    try:
        with measure.instrumentation.active():
            asyncio.run(measure.run())
    except (McpError, OSError, TimeoutError, RuntimeError, TypeError, ValueError) as error:
        measure.report.update(status="partial", reason=str(error))
        raise
    finally:
        measure.report["instrumentation"] = measure.instrumentation.report(
            max(
                0,
                measure.report["completed"]["raw_text_bytes"]
                - measure.report.get("resumed_confirmed", {}).get("raw_text_bytes", 0),
            )
        )
        measure.save()
    print(json.dumps({"report": str(output / "report.json"), "status": measure.report["status"]}))


if __name__ == "__main__":
    main()
