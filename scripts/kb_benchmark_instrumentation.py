"""Scoped product measurements for benchmarks; no serving hooks or configuration."""

from __future__ import annotations

import asyncio
import threading
import time
from collections import Counter, defaultdict, deque
from contextlib import ExitStack, contextmanager, suppress
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import patch

import apsw

from justpen_knowledgebase_mcp.storage.admission import DbAdmissionGate
from justpen_knowledgebase_mcp.storage.connection import ManagedConnection
from justpen_knowledgebase_mcp.storage.evidence import EvidenceStore
from justpen_knowledgebase_mcp.storage.maintenance import CheckpointMaintenance

if TYPE_CHECKING:
    from collections.abc import Generator

    from justpen_knowledgebase_mcp.service import KnowledgeBase
    from justpen_knowledgebase_mcp.storage.admission import ResetWindow
    from justpen_knowledgebase_mcp.storage.maintenance import WalState
    from justpen_knowledgebase_mcp.storage.worker import OperationToken


class Instrumentation:
    """Bound retained observations while counting every measured operation."""

    def __init__(self) -> None:
        """Keep last10000 observations per metric, never the full workload in RAM."""
        self.samples: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=10000))
        self.counts: Counter[str] = Counter()
        self.high_water: dict[str, int] = defaultdict(int)
        self.status_samples = 0
        self.pressure_episodes = 0
        self.was_pressure = False
        self.stopped = asyncio.Event()
        self.lock = threading.Lock()

    def observe(self, name: str, started: float) -> None:
        """Record elapsed wall time in milliseconds and uncapped observation count."""
        self.record(name, (time.perf_counter() - started) * 1000)

    def record(self, name: str, milliseconds: float) -> None:
        """Update sample/count atomically across worker and maintenance threads."""
        with self.lock:
            self.samples[name].append(milliseconds)
            self.counts[name] += 1

    def increment(self, name: str) -> None:
        """Count a non-duration event with the same synchronized authority."""
        with self.lock:
            self.counts[name] += 1

    def maximum(self, name: str, value: int) -> None:
        """Keep monotonic high-water observations across native owner threads."""
        with self.lock:
            self.high_water[name] = max(self.high_water[name], value)

    @contextmanager
    def active(self) -> Generator[None]:
        """Restore every instrumented class method even on timeout or cancellation."""
        with ExitStack() as stack:
            self._gates(stack)
            self._native(stack)
            yield

    def _gates(self, stack: ExitStack) -> None:
        transaction, reset = DbAdmissionGate.transaction, DbAdmissionGate.reset_window

        @contextmanager
        def shared(gate: DbAdmissionGate, token: OperationToken) -> Generator[None]:
            start = time.perf_counter()
            acquired = False
            try:
                with transaction(gate, token):
                    acquired = True
                    self.observe("shared_gate_acquisition", start)
                    yield
            finally:
                if not acquired:
                    self.observe("shared_gate_failed_acquisition", start)

        @contextmanager
        def exclusive(gate: DbAdmissionGate, mode: Literal["opportunistic", "pressure"]) -> Generator[ResetWindow]:
            start = time.perf_counter()
            admitted = False
            try:
                with reset(gate, mode) as window:
                    admitted = True
                    self.observe(mode + "_drain", start)
                    hold = time.perf_counter()
                    try:
                        yield window
                    finally:
                        self.observe(mode + "_exclusive_hold", hold)
            finally:
                self.increment(mode + ("_hit" if admitted else "_skip"))

        stack.enter_context(patch.object(DbAdmissionGate, "transaction", shared))
        stack.enter_context(patch.object(DbAdmissionGate, "reset_window", exclusive))

    def _native(self, stack: ExitStack) -> None:
        checkpoint = ManagedConnection.wal_checkpoint
        # Approved Task10 lifetime metric has no public hook; active() restores the scoped patch.
        publish = CheckpointMaintenance._publish  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        bucket = EvidenceStore.acquire_bucket

        def native(connection: ManagedConnection, dbname: str | None = None, mode: int = 0) -> tuple[int, int]:
            start = time.perf_counter()
            cpu = time.thread_time()
            try:
                result = checkpoint(connection, dbname, mode)
                if 0 <= result[1] <= result[0]:
                    self.maximum("checkpoint_backlog_frames", result[0] - result[1])
                else:
                    self.increment("checkpoint_invalid_statistics")
            except apsw.BusyError:
                self.increment("checkpoint_busy_mode" + str(mode))
                raise
            else:
                return result
            finally:
                self.observe("checkpoint_native_mode" + str(mode), start)
                name = "checkpoint_thread_cpu_mode" + str(mode)
                self.record(name, (time.thread_time() - cpu) * 1000)

        def publication(owner: CheckpointMaintenance, connection: ManagedConnection, state: WalState) -> None:
            start = time.perf_counter()
            try:
                publish(owner, connection, state)
            finally:
                self.observe("checkpoint_final_publication", start)

        def acquire(store: EvidenceStore, sha256: str, *, exclusive: bool, deadline: float) -> int:
            start = time.perf_counter()
            try:
                return bucket(store, sha256, exclusive=exclusive, deadline=deadline)
            finally:
                self.observe("evidence_bucket_acquisition", start)

        stack.enter_context(patch.object(ManagedConnection, "wal_checkpoint", native))
        stack.enter_context(patch.object(CheckpointMaintenance, "_publish", publication))
        stack.enter_context(patch.object(EvidenceStore, "acquire_bucket", acquire))

    async def sample(self, kb: KnowledgeBase) -> None:
        """Sample cached queues and WAL allocation every100ms, with no SQL workload."""
        wal = kb.workspace.db.with_name(kb.workspace.db.name + "-wal")
        while not self.stopped.is_set():
            state = await kb.status()
            for key, value in state["database_queues"].items():
                if isinstance(value, int) and not isinstance(value, bool):
                    self.maximum(key, value)
            for lane in ["short", "bulk"]:
                self.maximum(lane + "_backlog", state["io_queues"][lane]["pending"])
            pressure = state["wal"]["phase"] in {"pressure", "reset", "assessment_pending"}
            with self.lock:
                self.status_samples += 1
                if pressure and not self.was_pressure:
                    self.pressure_episodes += 1
                self.was_pressure = pressure
            with suppress(FileNotFoundError):
                self.maximum("allocated_wal_bytes", wal.stat().st_size)
            with suppress(TimeoutError):
                await asyncio.wait_for(self.stopped.wait(), 0.1)

    def report(self, logical_raw_bytes: int) -> dict[str, Any]:
        """Report retention and sampling limits beside measured summaries."""
        with self.lock:
            samples = {name: list(values) for name, values in self.samples.items()}
            counts, high_water = dict(self.counts), dict(self.high_water)
            pressure_episodes = self.pressure_episodes
            status_samples = self.status_samples
        latencies = {}
        for name, values in samples.items():
            ordered = sorted(values)
            latencies[name] = {
                "total_count": counts[name],
                "retained_count": len(ordered),
                **{
                    label: ordered[int((len(ordered) - 1) * percentile)]
                    for label, percentile in [("p50", 0.5), ("p95", 0.95), ("p99", 0.99)]
                },
            }
        return {
            "latency_ms": latencies,
            "counts": counts,
            "high_water": high_water,
            "status_samples": status_samples,
            "logical_raw_bytes_ingested_this_attempt": logical_raw_bytes,
            "pressure_episodes": pressure_episodes if status_samples else None,
            "pressure_episodes_per_ingested_gib": pressure_episodes / (logical_raw_bytes / 1073741824)
            if logical_raw_bytes and status_samples
            else None,
            "limits": [
                "cached100ms sampling may miss shorter pressure/backlog peaks",
                "deque percentiles cover most recent10000 samples, counts cover all",
                "wrappers add uncalibrated Python timing overhead",
                "native checkpoint timing includes native I/O; final publication and gate drain separate",
                "bucket/native durations include attempted operations, including failures; bucket duration is not a flock-contention count",
                "status sampling covers first corpus runtime only, not reopened runtime or lifecycle subprocesses",
            ],
        }
