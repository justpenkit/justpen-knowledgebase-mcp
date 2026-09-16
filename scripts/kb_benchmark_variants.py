"""Isolated SQLite comparison fixtures; none of these settings alter serving defaults."""

from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import apsw

if TYPE_CHECKING:
    from collections.abc import Generator


def check_deadline(deadline: float) -> None:
    """Interrupt offline audit work at the harness deadline, without serving policy changes."""
    if time.monotonic() >= deadline:
        raise TimeoutError("offline variant/audit deadline exceeded")


@contextmanager
def audit_connection(path: Path, deadline: float, *, readonly: bool = False) -> Generator[apsw.Connection]:
    """Close audit handles even if the bounded SQLite progress handler interrupts."""
    check_deadline(deadline)
    flags = apsw.SQLITE_OPEN_READONLY if readonly else apsw.SQLITE_OPEN_READWRITE | apsw.SQLITE_OPEN_CREATE
    connection = apsw.Connection(str(path), flags=flags)

    def progress() -> bool:
        check_deadline(deadline)
        return False

    connection.set_progress_handler(progress, 10000)
    try:
        yield connection
    finally:
        connection.close()


def percentiles(values: list[float]) -> dict[str, float]:
    """Lower-order-statistic percentiles: sorted[floor((n-1)*q)], in milliseconds."""
    ordered = sorted(values)
    return {
        name: ordered[int((len(ordered) - 1) * quantile)]
        for name, quantile in [("p50", 0.50), ("p95", 0.95), ("p99", 0.99)]
    }


def checkpoint_comparison(root: Path) -> list[dict[str, Any]]:
    """Same256x16KiB rows,16row transactions, WAL1000 versus explicit PASSIVE."""
    results: list[dict[str, Any]] = []
    for synchronous, automatic in [("FULL", 1000), ("FULL", 0), ("NORMAL", 0)]:
        path = root / f"checkpoint-{synchronous}-{automatic}.sqlite3"
        connection = apsw.Connection(str(path))
        connection.pragma("journal_mode", "wal")
        connection.pragma("synchronous", synchronous)
        connection.pragma("wal_autocheckpoint", automatic)
        connection.execute("create table corpus(id integer primary key, payload blob)")
        commits: list[float] = []
        reads: list[float] = []
        checkpoint_ms: list[float] = []
        checkpoints: list[tuple[int, int]] = []
        high_water = 0
        start_cpu = time.process_time()
        for batch in range(16):
            connection.execute("begin immediate")
            connection.executemany(
                "insert into corpus values(?,?)", ((batch * 16 + row, bytes([row]) * 16384) for row in range(16))
            )
            start = time.perf_counter()
            connection.execute("commit")
            commits.append((time.perf_counter() - start) * 1000)
            start = time.perf_counter()
            if connection.execute("select length(payload) from corpus where id=?", (batch * 16,)).get != 16384:
                raise RuntimeError("checkpoint corpus read mismatch")
            reads.append((time.perf_counter() - start) * 1000)
            high_water = max(high_water, Path(str(path) + "-wal").stat().st_size)
            if not automatic:
                start = time.perf_counter()
                checkpoints.append(connection.wal_checkpoint("main", apsw.SQLITE_CHECKPOINT_PASSIVE))
                checkpoint_ms.append((time.perf_counter() - start) * 1000)
        checkpoint_cpu_and_workload = time.process_time() - start_cpu
        page_size = connection.pragma("page_size")
        close_start = time.perf_counter()
        connection.close()
        results.append(
            {
                "synchronous": synchronous,
                "autocheckpoint_frames": automatic,
                "page_size": page_size,
                "transaction_rows": 16,
                "logical_raw_bytes": 4194304,
                "foreground_commit_ms": percentiles(commits),
                "read_ms": percentiles(reads),
                "passive_ms": percentiles(checkpoint_ms) if checkpoint_ms else None,
                "checkpoint_log_and_backfilled_frames": checkpoints,
                "wal_allocated_high_water": high_water,
                "workload_and_checkpoint_cpu_seconds": checkpoint_cpu_and_workload,
                "last_close_ms": (time.perf_counter() - close_start) * 1000,
                "policy": "legacy synchronous-per-batch PASSIVE on foreground connection; NOT separate-maintenance low-trigger experiment; does not measure product gates",
            }
        )
    return results


def worker_checkpoint(connection: apsw.Connection, trigger_frames: int) -> dict[str, Any]:
    """Time native PASSIVE on its separate owner thread, excluding setup/foreground work."""
    wall, cpu = time.perf_counter(), time.thread_time()
    busy = False
    frames: tuple[int, int] | None = None
    try:
        frames = connection.wal_checkpoint("main", apsw.SQLITE_CHECKPOINT_PASSIVE)
    except apsw.BusyError:
        busy = True
    return {
        "trigger_frames": trigger_frames,
        "worker_thread": threading.get_ident(),
        "wall_ms": (time.perf_counter() - wall) * 1000,
        "thread_cpu_ms": (time.thread_time() - cpu) * 1000,
        "log_backfilled_frames": frames,
        "busy": busy,
    }


def open_maintenance(owners: ExitStack, path: Path, deadline: float) -> apsw.Connection:
    """Create and configure the maintenance handle on its single owning thread."""
    owned = owners.enter_context(audit_connection(path, deadline))
    owned.pragma("synchronous", "FULL")
    owned.pragma("wal_autocheckpoint", 0)
    return owned


def observe_frames(state: list[int], _connection: apsw.Connection, _name: str, frames: int) -> int:
    """Observe committed WAL frames without doing checkpoint work in the writer callback."""
    state[0], state[1] = frames, max(state[1], frames)
    return 0


def checkpoint_worker_variant(
    root: Path, deadline: float, automatic: int, raw_bytes: int, low_bytes: int
) -> dict[str, Any]:
    """Keep native auto1000, or use a persistent separate maintenance owner for auto0."""
    path = root / f"worker-comparison-auto{automatic}.sqlite3"
    commits: list[float] = []
    reads: list[float] = []
    checkpoints: list[dict[str, Any]] = []
    pending: Future[dict[str, Any]] | None = None
    frame_state = [0, 0]
    allocated = 0
    digest = hashlib.sha256()
    foreground_thread = threading.get_ident()
    started, cpu_started = time.perf_counter(), time.process_time()
    with (
        audit_connection(path, deadline) as connection,
        ThreadPoolExecutor(max_workers=1) as pool,
        ExitStack() as owners,
    ):
        connection.pragma("journal_mode", "wal")
        connection.pragma("synchronous", "FULL")
        connection.pragma("wal_autocheckpoint", automatic)
        connection.execute("create table corpus(id integer primary key, payload blob)")
        page_size = connection.pragma("page_size")
        maintenance = None
        try:
            if not automatic:
                maintenance = pool.submit(open_maintenance, owners, path, deadline).result()
                # A WAL hook replaces SQLite's automatic hook, so install ONLY for auto0.
                connection.set_wal_hook(partial(observe_frames, frame_state))
            for first in range(0, raw_bytes // 16384, 16):
                check_deadline(deadline)
                rows = [(index, bytes([index % 256]) * 16384) for index in range(first, first + 16)]
                for _index, payload in rows:
                    digest.update(payload)
                connection.execute("begin immediate")
                connection.executemany("insert into corpus values(?,?)", rows)
                before = time.perf_counter()
                connection.execute("commit")
                commits.append((time.perf_counter() - before) * 1000)
                if pending is not None and pending.done():
                    checkpoints.append(pending.result())
                    pending = None
                if maintenance is not None and pending is None and frame_state[0] * page_size >= low_bytes:
                    pending = pool.submit(worker_checkpoint, maintenance, frame_state[0])
                before = time.perf_counter()
                if connection.execute("select length(payload) from corpus where id=?", (first,)).get != 16384:
                    raise RuntimeError("worker comparison readback mismatch")
                reads.append((time.perf_counter() - before) * 1000)
                allocated = max(allocated, Path(str(path) + "-wal").stat().st_size)
            if pending is not None:
                checkpoints.append(pending.result(timeout=max(0.001, deadline - time.monotonic())))
                pending = None
            count, total_bytes = connection.execute("select count(*),sum(length(payload)) from corpus").get
            if total_bytes != raw_bytes or connection.execute("pragma integrity_check").get != "ok":
                raise RuntimeError("checkpoint comparison corpus/integrity mismatch")
        finally:
            # Drain native work before closing its owned connection and the foreground handle.
            try:
                if pending is not None:
                    pending.result()
            finally:
                pool.submit(owners.close).result()
        close_started = time.perf_counter()
    return {
        "synchronous": "FULL",
        "autocheckpoint_frames": automatic,
        "transaction_rows": 16,
        "logical_raw_bytes": raw_bytes,
        "confirmed_rows": count,
        "corpus_sha256": digest.hexdigest(),
        "page_size": page_size,
        "low_trigger_bytes": low_bytes if not automatic else None,
        "max_observed_wal_frames": frame_state[1] if not automatic else None,
        "foreground_thread": foreground_thread,
        "triggered_checkpoints": checkpoints,
        "foreground_commit_ms": percentiles(commits),
        "read_ms": percentiles(reads),
        "wal_allocated_high_water": allocated,
        "integrity": "ok",
        "total_wall_seconds": time.perf_counter() - started,
        "total_process_cpu_seconds": time.process_time() - cpu_started,
        "last_close_ms": (time.perf_counter() - close_started) * 1000,
        "native_auto_checkpoint_wall_ms": None,
        "native_auto_checkpoint_thread_cpu_ms": None,
        "native_auto_timing_scope": "auto1000 checkpoint work is inseparable from native COMMIT timing; not measured separately",
        "schedule": "native SQLite auto1000"
        if automatic
        else "WAL-hook frame bytes >= low trigger, one outstanding PASSIVE on separate persistent connection/worker",
        "deadline_scope": "cooperative SQLite progress deadline; active native I/O is drained before closing, not a hard OS I/O deadline",
    }


def checkpoint_worker_comparison(
    root: Path, deadline: float, *, raw_bytes: int = 134217728, low_bytes: int = 67108864
) -> list[dict[str, Any]]:
    """Compare identical FULL corpora crossing the actual separate-maintenance trigger."""
    if raw_bytes < low_bytes * 2 or raw_bytes % 262144 or low_bytes <= 0:
        raise ValueError("checkpoint corpus must contain whole16-row batches and cross low trigger twice")
    check_deadline(deadline)
    root.mkdir(exist_ok=True)
    results = [checkpoint_worker_variant(root, deadline, automatic, raw_bytes, low_bytes) for automatic in [1000, 0]]
    completed = [
        item
        for item in results[1]["triggered_checkpoints"]
        if not item["busy"]
        and item["log_backfilled_frames"] is not None
        and item["log_backfilled_frames"][0] > 0
        and item["log_backfilled_frames"][1] > 0
    ]
    if not completed:
        raise RuntimeError("corpus failed to complete real separate low-trigger maintenance")
    return results


def fts_comparison(root: Path) -> list[dict[str, Any]]:
    """Canonical corpus stays available for verification in both index layouts."""
    results: list[dict[str, Any]] = []
    for contentless in [False, True]:
        path = root / f"fts-{contentless}.sqlite3"
        connection = apsw.Connection(str(path))
        connection.execute("create table canonical(id integer primary key, text)")
        option = "content='',contentless_delete=1" if contentless else "content='canonical',content_rowid='id'"
        connection.execute(
            f"create virtual table search using fts5(text,{option},tokenize='unicode61 remove_diacritics 0')"
        )
        start = time.perf_counter()
        with connection:
            for index in range(1000):
                text = f"record{index} alpha İstanbul beta " * 32
                connection.execute("insert into canonical values(?,?)", (index + 1, text))
                connection.execute("insert into search(rowid,text) values(?,?)", (index + 1, text))
        ingest_ms = (time.perf_counter() - start) * 1000
        queries: list[float] = []
        expected = []
        for _ in range(10):
            start = time.perf_counter()
            expected = connection.execute(
                "select rowid from search where search match 'alpha AND beta' order by rowid"
            ).fetchall()
            queries.append((time.perf_counter() - start) * 1000)
        if expected != [(index,) for index in range(1, 1001)]:
            raise RuntimeError("FTS corpus rowid mismatch")
        sizes = connection.execute("select name,sum(pgsize) from dbstat group by name").fetchall()
        connection.close()
        results.append(
            {
                "contentless_delete": contentless,
                "ingest_ms": ingest_ms,
                "query_ms": percentiles(queries),
                "database_bytes": path.stat().st_size,
                "tables": sizes,
                "parity": {
                    "rowids": True,
                    "snippet_literal_encoding_offsets_update_delete_reindex": "not established; no product migration authorized",
                },
                "identifier_index_added": False,
            }
        )
    return results


def backup_compaction(root: Path, source: Path, deadline: float) -> dict[str, Any]:
    """Copy a consistent SQLite snapshot and audit/compact it under the harness budget."""
    destination = root / "backup.sqlite3"
    with (
        audit_connection(source, deadline, readonly=True) as connection,
        audit_connection(destination, deadline) as copy,
    ):
        start = time.perf_counter()
        with copy.backup("main", connection, "main") as backup:
            while not backup.done:
                check_deadline(deadline)
                backup.step(256)
        duration = time.perf_counter() - start
        start = time.perf_counter()
        if copy.execute("pragma integrity_check").get != "ok":
            raise RuntimeError("backup integrity failure")
        integrity_seconds = time.perf_counter() - start
        before = destination.stat().st_size
        start = time.perf_counter()
        copy.execute("vacuum")
        compact = time.perf_counter() - start
    return {
        "backup_seconds": duration,
        "backup_bytes": before,
        "integrity_check": "ok",
        "integrity_seconds": integrity_seconds,
        "vacuum_seconds": compact,
        "compacted_bytes": destination.stat().st_size,
        "method": "SQLite online backup API; integrity and VACUUM on isolated snapshot, under harness deadline",
        "peak_additional_free_space": "not sampled; preflight reserves full backup and compaction",
    }


def split_database_comparison(root: Path) -> dict[str, Any]:
    """Measure a bounded ATTACH fixture without adding a serving database/API."""
    source = root / "split-graph.sqlite3"
    search = root / "split-search.sqlite3"
    connection = apsw.Connection(str(source))
    connection.pragma("journal_mode", "wal")
    connection.execute("attach database ? as search", (str(search),))
    connection.execute("pragma search.journal_mode=wal")
    connection.execute("create table main.owners(id integer primary key, epoch)")
    connection.execute("create virtual table search.documents using fts5(text)")
    start = time.perf_counter()
    with connection:
        for index in range(1000):
            connection.execute("insert into owners values(?,1)", (index,))
            connection.execute(
                "insert into search.documents(rowid,text) values(?,?)", (index, f"record{index} alpha beta")
            )
    transaction_ms = (time.perf_counter() - start) * 1000
    start = time.perf_counter()
    checkpoints = [connection.wal_checkpoint(name, apsw.SQLITE_CHECKPOINT_PASSIVE) for name in ["main", "search"]]
    checkpoint_ms = (time.perf_counter() - start) * 1000
    connection.close()
    return {
        "transaction_ms": transaction_ms,
        "checkpoint_ms": checkpoint_ms,
        "checkpoints": checkpoints,
        "graph_bytes": source.stat().st_size,
        "search_bytes": search.stat().st_size,
        "joint_atomic_commit": False,
        "recovery_epoch_multi_file_backup": "not implemented/equivalent; fixture cannot authorize a product split",
        "warning": "ATTACH in WAL does not guarantee atomic commit across both database files",
    }


def run_variants(root: Path, source: Path, deadline: float) -> dict[str, Any]:
    """Execute isolated comparisons after the product corpus has completed."""
    check_deadline(deadline)
    root.mkdir()
    return {
        "checkpoint": checkpoint_comparison(root),
        "checkpoint_worker_comparison": checkpoint_worker_comparison(root / "checkpoint-worker", deadline),
        "fts": fts_comparison(root),
        "backup_compaction": backup_compaction(root, source, deadline),
        "split_database": split_database_comparison(root),
    }
