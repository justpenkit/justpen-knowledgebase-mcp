"""Idle job-lane instruments; scoped measurement only, never a product policy change."""

from __future__ import annotations

import asyncio
import threading
import time
from collections import Counter
from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.jobs import JobRunner
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.jobs import JobStore

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Generator
    from pathlib import Path

    import apsw
    from benchmark_knowledgebase import Measurements

    from justpen_knowledgebase_mcp.storage.jobs import Claim
    from justpen_knowledgebase_mcp.storage.worker import DatabaseWorkers, OperationToken

    Callback = Callable[[apsw.Connection, OperationToken], object]
    WorkerCall = Callable[[Callback, OperationToken | None], Awaitable[object]]

LANES = ["read", "write", "control"]
SETTLE_SECONDS = 3.0
IDLE_SECONDS = 10.0
WAIT_SAMPLES = 20
RETAINED_TERMINALS = 1024

IDLE_LIMITS = [
    "one lane submission is exactly one BEGIN/COMMIT; the counter observes submissions, not retries inside SQLite",
    "counted on submission, so a transaction that fails to commit is still counted as issued",
    "the window is a fixed wall-clock sleep; a busy host lowers the observed rate without any product change",
    "cached status sampling is excluded because kb.status() admits no database work",
]
WAIT_LIMITS = [
    "the terminal stamp is taken when the terminal UPDATE is issued, before its COMMIT, so the lag includes that commit",
    "wait_ms spans admission plus the durable step plus the observation; observation_lag_ms isolates the poll",
    "a sample whose terminal transition was not observed in-process is reported unpaired, never as zero lag",
]


@contextmanager
def lane_transactions(workers: DatabaseWorkers) -> Generator[Counter[str]]:
    """Count issued lane submissions; each one is exactly one guarded transaction."""
    counts: Counter[str] = Counter()
    with ExitStack() as stack:
        for lane, original in [("read", workers.read), ("write", workers.write), ("control", workers.control)]:

            async def issued(
                callback: Callback,
                token: OperationToken | None = None,
                *,
                lane: str = lane,
                original: WorkerCall = original,
            ) -> object:
                counts[lane] += 1
                return await original(callback, token)

            stack.enter_context(patch.object(workers, lane, issued))
        yield counts


@contextmanager
def wait_observation() -> Generator[list[dict[str, float | None]]]:
    """Pair every terminal job transition with the moment its waiter observed it."""
    observations: list[dict[str, float | None]] = []
    terminals: dict[str, float] = {}
    lock = threading.Lock()
    finish, finish_failure, wait = JobStore.finish, JobStore.finish_failure, JobRunner.wait

    def stamp(job_id: str) -> None:
        with lock:
            terminals[job_id] = time.perf_counter()
            if len(terminals) > RETAINED_TERMINALS:
                terminals.pop(next(iter(terminals)))

    def finished(
        connection: apsw.Connection,
        claim: Claim,
        state: str,
        result: dict[str, Any],
        *,
        now: float | None = None,
    ) -> None:
        finish(connection, claim, state, result, now=now)
        stamp(claim.job_id)

    def failed(connection: apsw.Connection, claim: Claim, state: str, result: dict[str, Any]) -> None:
        finish_failure(connection, claim, state, result)
        stamp(claim.job_id)

    async def observed(
        runner: JobRunner, job_id: str, deadline: float, accepted: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        started = time.perf_counter()
        result = await wait(runner, job_id, deadline, accepted)
        returned = time.perf_counter()
        with lock:
            terminal = terminals.pop(job_id, None)
        observations.append(
            {
                "wait_ms": (returned - started) * 1000,
                "observation_lag_ms": None if terminal is None else (returned - terminal) * 1000,
            }
        )
        return result

    with ExitStack() as stack:
        stack.enter_context(patch.object(JobStore, "finish", finished))
        stack.enter_context(patch.object(JobStore, "finish_failure", failed))
        stack.enter_context(patch.object(JobRunner, "wait", observed))
        yield observations


async def job_states(kb: KnowledgeBase) -> dict[str, Any]:
    """Read the durable job states directly, outside any counted window."""
    rows = await kb.workers.read(lambda c, _t: c.execute("select state,count(*) from jobs group by state").fetchall())
    return {str(state): count for state, count in rows}


async def idle_control_rate(
    kb: KnowledgeBase, deadline: float, seconds: float = IDLE_SECONDS, settle: float = SETTLE_SECONDS
) -> dict[str, Any]:
    """Count the transactions an otherwise idle server issues per lane per second."""
    await asyncio.sleep(max(0.0, min(settle, deadline - time.monotonic())))
    window = max(0.0, min(seconds, deadline - time.monotonic()))
    if window <= 0:
        raise TimeoutError("no budget left for the idle lane window")
    before = await job_states(kb)
    with lane_transactions(kb.workers) as counts:
        started = time.monotonic()
        await asyncio.sleep(window)
        elapsed = time.monotonic() - started
        observed = dict(counts)
    after = await job_states(kb)
    pending = {
        state: count for state, count in (before | after).items() if state not in {"completed", "failed", "cancelled"}
    }
    return {
        "window_seconds": elapsed,
        "settle_seconds": settle,
        "jobs_by_state_before": before,
        "jobs_by_state_after": after,
        "idle": not pending,
        "transactions": observed,
        "transactions_per_second": {lane: observed.get(lane, 0) / elapsed for lane in LANES},
        "limits": IDLE_LIMITS,
    }


async def job_wait_samples(kb: KnowledgeBase, deadline: float, samples: int = WAIT_SAMPLES) -> dict[str, Any]:
    """Drive bounded inline ingests so each waiter observes one terminal transition."""
    with wait_observation() as observations:
        for index in range(samples):
            if time.monotonic() >= deadline:
                break
            await kb.ingest_evidence({"text": f"job wait probe {index}", "media_type": "text/plain"})
    lags = [item["observation_lag_ms"] for item in observations]
    return {
        "requested_samples": samples,
        "observed_waits": len(observations),
        "driver": "inline kb_ingest_evidence; JobRunner.wait is entered by the product, not by the harness",
        "observation_lag_ms_samples": [value for value in lags if value is not None],
        "unpaired_waits": sum(1 for value in lags if value is None),
        "wait_ms_samples": [item["wait_ms"] for item in observations if item["wait_ms"] is not None],
        "limits": WAIT_LIMITS,
    }


async def run_lanes(measure: Measurements, root: Path) -> dict[str, Any]:
    """Measure both job-lane instruments on a dedicated workspace with no corpus."""
    measure.check()
    await asyncio.to_thread(root.mkdir, parents=True)
    async with KnowledgeBase.open(ServerConfig(workspace_dir=root)) as kb:
        idle = await idle_control_rate(kb, measure.deadline)
        measure.check()
        waits = await job_wait_samples(kb, measure.deadline)
    return {"idle_control_transactions": idle, "job_wait": waits}
