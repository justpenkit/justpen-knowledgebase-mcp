"""Bounded product lifecycle experiments, separate from corpus generation and policy."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import resource
import select
import stat
import sys
import time
from collections import Counter
from contextlib import AsyncExitStack, ExitStack, contextmanager
from pathlib import Path
from types import FunctionType
from typing import TYPE_CHECKING, Any
from unittest.mock import patch
from uuid import uuid4

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import BusyError, LimitError, McpError
from justpen_knowledgebase_mcp.identity import identity_key
from justpen_knowledgebase_mcp.mutations import canonical_json
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.job_recovery import recover_intents
from justpen_knowledgebase_mcp.storage.job_retention import JobRetention
from justpen_knowledgebase_mcp.storage.jobs import PENDING_SQL, JobStore
from justpen_knowledgebase_mcp.storage.worker import OperationToken

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Generator

    import apsw
    from benchmark_knowledgebase import Measurements

    Callback = Callable[[apsw.Connection, OperationToken], object]
    WorkerCall = Callable[[Callback, OperationToken | None], Awaitable[object]]

ROOT = Path(__file__).resolve().parents[1]


def rss_bytes() -> int:
    """Report reaped-child high-water RSS, not a falsely summed process sample."""
    return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * (1 if sys.platform == "darwin" else 1024)


async def response_data(peer: Client[StdioTransport], name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Translate BUSY/LIMIT envelopes so failed responses get separate timing samples."""
    response = await peer.call_tool(name, arguments, raise_on_error=False)
    value = response.structured_content
    if value is None:
        raise RuntimeError("missing structured response")
    if value["status"] == "ok":
        return value["data"]
    error = value["error"]
    if error.startswith("BUSY:"):
        raise BusyError(error)
    if error.startswith("LIMIT:"):
        raise LimitError(error)
    raise RuntimeError(str(value))


@contextmanager
def committed_calls(kb: KnowledgeBase, report: dict[str, Any]) -> Generator[None]:
    """Count completed guarded writes, separating empty claim polling and failed calls."""
    counts: Counter[str] = Counter()
    failed: Counter[str] = Counter()
    names = ["admit_delete", "delete_step", "heartbeat", "claim", "finish", "checkpoint", "release"]
    report["committed_calls"] = counts
    report["failed_calls"] = failed
    with ExitStack() as stack:
        for lane, original in [("write", kb.workers.write), ("control", kb.workers.control)]:

            async def observe(
                callback: Callback, token: OperationToken | None = None, *, original: WorkerCall = original
            ) -> object:
                operations = callback.__code__.co_names if isinstance(callback, FunctionType) else ()
                category = next((name for name in names if name in operations), "other")
                try:
                    result = await original(callback, token)
                except BaseException:
                    failed[category] += 1
                    raise
                counts["empty_claim_poll" if category == "claim" and result is None else category] += 1
                return result

            stack.enter_context(patch.object(kb.workers, lane, observe))
        yield


async def clients(measure: Measurements, root: Path) -> None:
    """Run real stdio callers; latency includes transport and committed response."""
    outcomes = measure.report.setdefault("lifecycle", {})
    for count in [1, 4, 8]:
        measure.check()
        result: dict[str, Any] = {"clients": count, "rounds_per_client": 10, "confirmed_writes": 0, "errors": {}}
        outcomes[f"clients_{count}"] = result
        env = {key: value for key, value in os.environ.items() if not key.startswith(("OTEL_", "JUSTPEN_"))}
        env.update(
            JUSTPEN_KNOWLEDGEBASE_WORKSPACE_DIR=str(root),
            PYTHONDONTWRITEBYTECODE="1",
            FASTMCP_SHOW_SERVER_BANNER="false",
        )
        async with AsyncExitStack() as stack:
            peers = [
                await stack.enter_async_context(
                    Client(
                        StdioTransport(command=sys.executable, args=["-B", "-m", "justpen_knowledgebase_mcp"], env=env)
                    )
                )
                for _ in range(count)
            ]

            async def requests(
                index: int, peer: Client[StdioTransport], count: int = count, result: dict[str, Any] = result
            ) -> None:
                for sequence in range(10):
                    measure.check()
                    try:
                        value = await measure.call(
                            f"clients{count}_write",
                            response_data(
                                peer,
                                "kb_write",
                                {
                                    "nodes": [
                                        {
                                            "type": "hostname",
                                            "properties": {"name": f"client-{count}-{index}-{sequence}.example"},
                                        }
                                    ]
                                },
                            ),
                        )
                    except (BusyError, LimitError) as error:
                        key = error.error_type
                        result["errors"][key] = result["errors"].get(key, 0) + 1
                        continue
                    result["confirmed_writes"] += 1
                    identifier = value["nodes"][0]["id"]
                    read = await measure.call(
                        f"clients{count}_read", response_data(peer, "kb_get", {"kind": "nodes", "ids": [identifier]})
                    )
                    if read["records"][0]["id"] != identifier:
                        raise RuntimeError("committed stdio write/read mismatch")

            async with asyncio.timeout(min(120, max(0.1, measure.deadline - time.monotonic()))):
                await asyncio.gather(*(requests(index, peer) for index, peer in enumerate(peers)))
        result["reaped_child_max_rss_bytes"] = rss_bytes()
        result["rss_population"] = (
            "cumulative child high-water over completed subprocesses, not sum/concurrent working set"
        )
        result["max_confirmed_write_response_ms"] = max(
            measure.latencies.get(f"clients{count}_write", []), default=None
        )
        result["writer_starvation_scope"] = (
            "bounded10writes per caller; observed maximum response delay, no starvation-free scheduling SLA"
        )
        measure.save()


async def hub(measure: Measurements, root: Path) -> None:
    """Measure full normal JobRunner completion on a fixed100K-incident-edge hub."""
    edges = 1000 if measure.scale == "smoke" else 100000
    report: dict[str, Any] = {
        "incident_relations": edges,
        "drive": "normal JobRunner through KnowledgeBase.delete",
        "status": "preparing",
    }
    measure.report.setdefault("lifecycle", {})["hub"] = report
    async with KnowledgeBase.open(ServerConfig(workspace_dir=root)) as kb:
        setup_started = time.perf_counter()
        nodes = await kb.write(
            {"nodes": [{"type": "endpoint", "properties": {"url": "https://hub.example/", "method": "GET"}}]}
        )
        owner = nodes["nodes"][0]["id"]
        relation_key = identity_key("relations", "redirects_to", {"context": "hub-fixture"})
        for start in range(0, edges, 1000):
            measure.check()

            def populate(c: apsw.Connection, _t: OperationToken, start: int = start) -> None:
                source_id = c.execute("select id from nodes where uuid=?", (owner,)).get
                for index in range(start, min(start + 1000, edges)):
                    properties = {"url": f"https://leaf.example/{index}", "method": "GET"}
                    c.execute(
                        "insert into nodes(uuid,type,key,properties) values(?,'endpoint',?,?)",
                        (str(uuid4()), identity_key("nodes", "endpoint", properties), canonical_json(properties)),
                    )
                    target_id = c.last_insert_rowid()
                    c.execute(
                        "insert into relations(uuid,source_id,target_id,type,key,properties) values(?,?,?,'redirects_to',?,?)",
                        (str(uuid4()), source_id, target_id, relation_key, canonical_json({"context": "hub-fixture"})),
                    )

            await kb.workers.write(populate)
        report["setup_seconds"] = time.perf_counter() - setup_started
        report["fixture"] = (
            "distinct endpoint targets (incident_relations count) and catalog identity keys in1000-row guarded setup transactions; derived indexes omitted; setup is not product ingestion throughput"
        )
        report["status"] = "pending"
        started = time.perf_counter()
        with committed_calls(kb, report):
            accepted = await measure.call(
                "hub_delete_admission_and_wait", kb.delete({"kind": "nodes", "ids": [owner], "cascade": True})
            )
            result = accepted
            phase_deadline = min(measure.deadline, time.monotonic() + 180)
            while result["state"] not in {"completed", "failed", "cancelled"} and time.monotonic() < phase_deadline:
                measure.check()
                result = await kb.job_runner.wait(
                    accepted["job_id"], min(phase_deadline, time.monotonic() + 1), accepted
                )
                report.update(
                    status=result["state"],
                    progress=result.get("progress", {}),
                    elapsed_seconds=time.perf_counter() - started,
                )
                measure.save()
            report.update(status=result["state"], pending_seconds=time.perf_counter() - started, final=result)
        report["remaining_relations"] = await kb.workers.read(
            lambda c, _t: c.execute("select count(*) from relations").get
        )
        steps = report["committed_calls"]["delete_step"]
        report["commits_per_cleanup_step"] = (
            (
                report["committed_calls"]["admit_delete"]
                + report["committed_calls"]["claim"]
                + steps
                + report["committed_calls"]["heartbeat"]
            )
            / steps
            if steps
            else None
        )
        report["commit_semantics"] = (
            "delete_step includes cleanup/progress and lease-yield or terminal finalize in ONE commit; no fixed3000-FULL-commit assumption; empty claim polling and other maintenance separate"
        )
        if result["state"] == "completed" and report["remaining_relations"] != 0:
            raise RuntimeError("completed hub retained incident edges")
        measure.save()


async def recovery(measure: Measurements, root: Path) -> None:
    """Measure sparse intent and primary-key readiness against1M actual ready rows."""
    ready = 1000 if measure.scale == "smoke" else 1000000
    report: dict[str, Any] = {
        "ready_rows": ready,
        "drive": "manual isolated JobStore recovery primitives",
        "status": "preparing",
    }
    measure.report.setdefault("lifecycle", {})["recovery"] = report
    async with KnowledgeBase.open(ServerConfig(workspace_dir=root)) as kb:
        await kb.job_runner.close()
        for start in range(0, ready, 10000):
            measure.check()
            await kb.workers.control(
                lambda c, _t, start=start: c.execute(
                    "with recursive n(x) as (values(?) union all select x+1 from n where x<?) insert into nodes(uuid,type,key,properties) select printf('%036d',x),'hostname',cast(x as text),'{}' from n",
                    (start + 1, min(start + 10000, ready)),
                ).fetchall()
            )
        job_ids = [str(uuid4()) for _ in range(101)]

        def pending(c: apsw.Connection, _t: OperationToken) -> None:
            for index, job_id in enumerate(job_ids):
                identifier = str(uuid4())
                c.execute(
                    "insert into nodes(uuid,type,key,properties,lifecycle,delete_job_id,delete_cascade,delete_requested_at) values(?,'hostname',?,'{}','delete_pending',?,1,1)",
                    (identifier, identifier, job_id),
                )
                if index < 100:
                    JobStore.insert(c, job_id, "fixture", "short", {})

        await kb.workers.control(pending)
        report["plans"] = await kb.workers.read(
            lambda c, _t: {
                "pending": c.execute("explain query plan " + PENDING_SQL["nodes"], (0,)).fetchall(),
                "endpoint_ready": c.execute(
                    "explain query plan select lifecycle from nodes where id=?", (ready,)
                ).fetchall(),
            }
        )
        first = await measure.call(
            "sparse_recovery_first100", kb.workers.control(lambda c, _t: recover_intents(c, "nodes", 0))
        )
        second = await measure.call(
            "sparse_recovery_owner101", kb.workers.control(lambda c, _t: recover_intents(c, "nodes", first["after_id"]))
        )
        if first["repaired"] != 0 or second["repaired"] != 1:
            raise RuntimeError("sparse recovery missed owner101")
        for _ in range(10):
            await measure.call(
                "million_ready_endpoint_pk",
                kb.workers.read(lambda c, _t: c.execute("select lifecycle from nodes where id=?", (ready,)).get),
            )
        report.update(status="completed", first=first, second=second)
        await retention(measure, kb, report)
        measure.save()


async def retention(measure: Measurements, kb: KnowledgeBase, report: dict[str, Any]) -> None:
    """Measure terminal counters and100-row pruning under the existing policy."""

    def seed(c: apsw.Connection, _t: OperationToken) -> None:
        c.executemany(
            "insert into jobs(uuid,kind,lane,state,requested_at,updated_at,finished_at,payload) values(?,'fixture','short','completed',1,1,1,'{}')",
            ((str(uuid4()),) for _ in range(1000)),
        )
        JobRetention.reconcile(c)

    await kb.workers.control(seed)
    before = await measure.call("terminal_counter_read", kb.workers.read(lambda c, _t: JobRetention.counts(c)))
    cursor = (0.0, 0)
    total = commits = 0
    while True:
        measure.check()
        batch = await measure.call(
            "terminal_prune_batch",
            kb.workers.control(
                lambda c, _t, cursor=cursor: JobRetention.batch(c, kb.workers.factory.guard.policy(c), cursor)
            ),
        )
        commits += 1
        total += batch["pruned"]
        cursor = batch["cursor"]
        if cursor == (0.0, 0):
            break
    report["retention"] = {
        "before": before,
        "pruned": total,
        "commits": commits,
        "after": await kb.workers.read(lambda c, _t: JobRetention.counts(c)),
    }
    if total != 1000:
        raise RuntimeError("bounded prune failed to remove1000expired jobs")


async def contention(measure: Measurements, root: Path) -> None:
    """Measure real flock collision and writer-blocked heartbeat without policy changes."""
    report: dict[str, Any] = {
        "drive": "manual contention primitives; heartbeat interval unchanged",
        "bucket_count": 4096,
    }
    measure.report.setdefault("lifecycle", {})["contention"] = report
    async with KnowledgeBase.open(ServerConfig(workspace_dir=root)) as kb:
        await kb.job_runner.close()
        store = kb.job_runner.store
        original = fcntl.flock
        collisions = 0

        def flock(fd: int, operation: int) -> None:
            nonlocal collisions
            try:
                original(fd, operation)
            except BlockingIOError:
                if operation & fcntl.LOCK_EX:
                    collisions += 1
                raise

        with (
            store.bucket("abc" + "0" * 61, exclusive=False, deadline=time.monotonic() + 1),
            patch.object(fcntl, "flock", flock),
        ):
            try:
                await measure.call(
                    "bucket_collision_deadline",
                    asyncio.to_thread(
                        store.acquire_bucket, "abc" + "1" * 61, exclusive=True, deadline=time.monotonic() + 0.05
                    ),
                )
            except BusyError:
                pass
            else:
                raise RuntimeError("same bucket exclusive lock bypassed reader")
        report["bucket_blocking_flock_attempts"] = collisions
        job_id = str(uuid4())
        await kb.workers.control(lambda c, _t: JobStore.insert(c, job_id, "fixture", "bulk", {}))
        claim = await kb.workers.control(lambda c, _t: JobStore.claim(c, "bulk", "fixture"))
        if claim is None:
            raise RuntimeError("heartbeat fixture claim missing")
        writer = kb.workers.factory.open_writer()
        writer.execute("begin immediate")
        writer.execute("update settings set query_epoch=query_epoch+1")
        started = time.perf_counter()
        try:
            try:
                await measure.call(
                    "heartbeat_blocked_attempt",
                    kb.workers.control(
                        lambda c, _t: JobStore.heartbeat(c, claim), OperationToken(time.monotonic() + 0.05)
                    ),
                )
            except (BusyError, LimitError):
                report["heartbeat_failed_attempts"] = 1
            else:
                raise RuntimeError("heartbeat bypassed real writer lock")
        finally:
            writer.execute("rollback")
            writer.close()
        renewed, _cancelled = await measure.call(
            "heartbeat_after_release", kb.workers.control(lambda c, _t: JobStore.heartbeat(c, claim))
        )
        await kb.workers.read(lambda c, _t: JobStore.fence(c, claim))
        report.update(
            heartbeat_delay_seconds=time.perf_counter() - started,
            renewed_after_original_expiry=renewed > claim.expires_at,
            lease_lost=False,
        )
        measure.save()


async def copy_takeover(measure: Measurements, root: Path) -> None:
    """Reuse the process-kill failpoint and measure actual byte-zero replacement copy."""
    raw = bytes(range(256)) * 8192
    (root / "input.bin").write_bytes(raw)
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    script = ROOT / "tests/integration/job_worker.py"
    started = time.perf_counter()
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        str(script),
        str(root),
        "partial_copy",
        str(ready_write),
        str(release_read),
        pass_fds=(ready_write, release_read),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    os.close(ready_write)
    os.close(release_read)
    try:
        signalled = await asyncio.to_thread(select.select, [ready_read], [], [], 15)
        if not signalled[0] or os.read(ready_read, 1) != b"1":
            raise RuntimeError("partial-copy barrier did not arrive")
        stages = await asyncio.to_thread(lambda: list(root.rglob("*.stage")))
        if len(stages) != 1 or stages[0].read_bytes() != raw[:65536]:
            raise RuntimeError("partial-copy fixture did not stop at64KiB")
        child.kill()
        await asyncio.wait_for(child.wait(), 10)
        report: dict[str, Any] = {
            "first_attempt_bytes": 65536,
            "first_attempt_startup_to_kill_seconds": time.perf_counter() - started,
            "attempts": 2,
            "test_acceleration": "durable lease expiry moved to0 after killing its owner; lease policy unchanged",
        }
        measure.report.setdefault("lifecycle", {})["copy_takeover"] = report
    finally:
        if child.returncode is None:
            child.kill()
        await asyncio.wait_for(child.communicate(), 10)
        os.close(ready_read)
        os.close(release_write)
    copied = 0
    first_piece = b""
    native_write = os.write

    def write(fd: int, content: bytes) -> int:
        nonlocal copied, first_piece
        size = native_write(fd, content)
        if stat.S_ISREG(os.fstat(fd).st_mode):
            copied += size
            if not first_piece:
                first_piece = bytes(content[:size])
        return size

    async with KnowledgeBase.open(ServerConfig(workspace_dir=root)) as kb:
        job_id = await kb.workers.control(lambda c, _t: c.execute("select uuid from jobs").get)
        await kb.workers.control(lambda c, _t: c.execute("update jobs set lease_expires_at=0").fetchall())
        started = time.perf_counter()
        with patch.object(os, "write", write):
            kb.job_runner.wake("bulk")
            result = await kb.job_runner.wait(job_id, min(measure.deadline, time.monotonic() + 30))
        report.update(
            second_attempt_seconds=time.perf_counter() - started,
            second_attempt_bytes=copied,
            physical_copy_bytes=65536 + copied,
            result=result["state"],
        )
        if result["state"] != "completed" or copied != len(raw) or first_piece != raw[:65536]:
            raise RuntimeError("takeover did not recopy full source from byte0")
        if (
            result["evidence_id"] != "e_" + hashlib.sha256(raw).hexdigest()
            or (root / "input.bin").read_bytes() != raw
            or stages[0].exists()
        ):
            raise RuntimeError("takeover hash/source/staging invariant failed")
    measure.save()


async def run_lifecycle(measure: Measurements) -> None:
    """Retain each phase result immediately; global deadline and disk reserve stay authoritative."""
    for name, phase in [
        ("clients", clients),
        ("hub", hub),
        ("recovery", recovery),
        ("contention", contention),
        ("copy_takeover", copy_takeover),
    ]:
        measure.check()
        root = measure.output / name
        root.mkdir()
        try:
            await phase(measure, root)
        except (McpError, OSError, TimeoutError, RuntimeError) as error:
            measure.report.setdefault("lifecycle", {})[name + "_failure"] = str(error)
            measure.save()
            raise
    if measure.report.get("lifecycle", {}).get("hub", {}).get("status") != "completed":
        raise TimeoutError("hub cleanup did not complete within its phase budget; other phase results retained")
