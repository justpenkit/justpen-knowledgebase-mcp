"""Shared maintenance policy, cooldown and real WAL pressure behavior."""

import asyncio
import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, nullcontext

import apsw
import pytest

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import BusyError, ConfigurationError, LimitError, WalBusyError
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.admission import DbAdmissionGate
from justpen_knowledgebase_mcp.storage.connection import ManagedConnection, SQLiteRuntime
from justpen_knowledgebase_mcp.storage.maintenance import CheckpointMaintenance
from justpen_knowledgebase_mcp.storage.worker import OperationToken
from justpen_knowledgebase_mcp.workspace import WorkspacePaths

pytestmark = pytest.mark.integration


async def test_service_starts_with_valid_sample_and_status_never_opens_sql(kb):
    assert hasattr(kb, "maintenance"), "lifespan must own a dedicated maintenance worker"
    status = kb.maintenance.status()
    assert status["phase"] == "normal"
    assert status["sample_at"] <= time.time()
    assert status["estimated_completion_ms"] is None
    assert status["retry_after_ms"] >= 1000
    assert await kb.workers.read(lambda c, t: c.execute("select 1").get) == 1


async def test_latest_pressure_blocks_all_product_lanes_but_control_survives(kb):
    def pressure(c, t):
        state = json.loads(c.execute("select maintenance from settings").get)
        state["phase"] = "pressure"
        state["pressure_started_at"] = time.time()
        c.execute("update settings set maintenance=?", (json.dumps(state),))

    await kb.workers.control(pressure)
    for operation in (kb.workers.read, kb.workers.write):
        with pytest.raises(BusyError, match="WAL_PRESSURE"):
            await operation(lambda c, t: pytest.fail("product callback must not run"))
    assert await kb.workers.control(lambda c, t: c.execute("select 2").get) == 2


async def test_stale_normal_is_admitted_without_new_sample_sequence(kb):
    def age(c, t):
        state = json.loads(c.execute("select maintenance from settings").get)
        state["sample_at"] = time.time() - 100
        c.execute("update settings set maintenance=?", (json.dumps(state),))

    await kb.workers.control(age)
    assert await kb.workers.read(lambda c, t: c.execute("select 3").get) == 3
    assert kb.maintenance.status()["stale_normal"] is True


@pytest.mark.parametrize("bad", [{}, {"format_version": 2}])
def test_invalid_persisted_policy_rejected_on_reopen(tmp_path, bad):
    cfg = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(cfg) as ws, SQLiteRuntime(ws, cfg) as runtime:
        c = runtime.connect()
        c.execute("update settings set policy=?", (json.dumps(bad),))
        c.close()
        with pytest.raises(ConfigurationError, match="policy"):
            runtime.connect()


def test_shared_cooldown_and_backward_clock_jump(small_wal):
    _factory, maintenance, c = small_wal
    first = maintenance.run_once("startup")
    assert first["last_attempt"] == "restart"
    again = maintenance.run_once("pressure")
    assert again["attempt_id"] == first["attempt_id"]
    assert again["sample_seq"] == first["sample_seq"]
    state = json.loads(c.execute("select maintenance from settings").get)
    state["next_attempt_not_before"] = time.time() + 60
    c.execute("update settings set maintenance=?", (json.dumps(state),))
    corrected = maintenance.run_once("timer")
    assert corrected["sample_seq"] == first["sample_seq"]
    assert time.time() < corrected["next_attempt_not_before"] <= time.time() + 1


def test_pinned_reader_backlog_pressure_recovers_after_reader_release(small_wal):
    factory, maintenance, writer = small_wal
    maintenance.run_once("startup")
    writer.execute("create table payload(value blob)")
    reader = factory.open_reader()
    reader.execute("begin")
    reader.execute("select * from payload").fetchall()
    try:
        writer.execute("insert into payload values(zeroblob(131072))")
        time.sleep(1.05)
        pressured = maintenance.run_once("pressure")
        assert pressured["phase"] == "pressure"
        assert pressured["unbackfilled_bytes"] >= 65536
        assert pressured["last_attempt"] == "skipped_busy"
    finally:
        reader.execute("rollback")
        reader.close()
    time.sleep(1.05)
    recovered = maintenance.run_once("pressure")
    assert recovered["phase"] == "normal"
    assert recovered["last_attempt"] == "restart"
    assert recovered["unbackfilled_bytes"] <= 16384


def test_rolling_reader_zero_backlog_does_not_prove_reuse(small_wal, monkeypatch):
    factory, maintenance, writer = small_wal
    maintenance.run_once("startup")
    writer.execute("create table payload(value blob)")
    writer.execute("insert into payload values(zeroblob(131072))")
    reader = factory.open_reader()
    real = ManagedConnection.wal_checkpoint

    def checkpoint(self, dbname=None, mode=0):
        if mode == 0:
            reader.execute("begin")
            reader.execute("select length(value) from payload").fetchall()
        return real(self, dbname, mode)

    monkeypatch.setattr(ManagedConnection, "wal_checkpoint", checkpoint)
    try:
        time.sleep(1.05)
        snapshot = maintenance.run_once("pressure")
        assert snapshot["unbackfilled_bytes"] == 0
        assert snapshot["phase"] == "pressure"
        assert snapshot["last_attempt"] == "skipped_busy"
    finally:
        reader.execute("rollback")
        reader.close()


@pytest.mark.parametrize(
    ("field", "value"), [("sample_at", -1.0), ("sample_at", 1e20), ("log_frames", -1), ("page_size", 0)]
)
async def test_invalid_samples_never_admit_products(kb, field, value):
    def invalidate(c, t):
        state = json.loads(c.execute("select maintenance from settings").get)
        state[field] = value
        c.execute("update settings set maintenance=?", (json.dumps(state),))

    await kb.workers.control(invalidate)
    with pytest.raises(BusyError, match="WAL_PRESSURE"):
        await kb.workers.read(lambda c, t: pytest.fail("invalid sample admitted"))


def test_invalid_restart_statistics_are_not_reuse_proof(small_wal, monkeypatch):

    _, maintenance, _ = small_wal
    real = ManagedConnection.wal_checkpoint

    def checkpoint(self, dbname=None, mode=0):
        if mode == 2:
            return -1, -1
        return real(self, dbname, mode)

    monkeypatch.setattr(ManagedConnection, "wal_checkpoint", checkpoint)
    snapshot = maintenance.run_once("startup")
    assert snapshot["phase"] == "unknown"
    assert snapshot["last_attempt"] != "restart"
    assert snapshot["checkpoint_mode"] == "RESTART"


def test_commit_wake_uses_shared_policy_threshold(small_wal):
    factory, _maintenance, writer = small_wal
    writer.execute("create table payload(value blob)")
    writer.execute("insert into payload values(zeroblob(32768))")
    wakes = []
    factory.wake_maintenance = wakes.append
    factory.committed(writer)
    assert wakes == ["low"]


def test_large_reusable_allocation_does_not_trigger_low_reset(small_wal, monkeypatch):
    factory, maintenance, writer = small_wal
    maintenance.run_once("startup")
    writer.pragma("journal_size_limit", -1)
    writer.execute("create table payload(value blob)")
    writer.execute("insert into payload values(zeroblob(32768))")
    writer.wal_checkpoint("main", 2)
    writer.execute("update settings set query_epoch=query_epoch+1")
    assert 16384 < factory.workspace.db.with_name("graph.sqlite3-wal").stat().st_size < 65536
    real = ManagedConnection.wal_checkpoint
    modes = []

    def checkpoint(self, dbname=None, mode=0):
        modes.append(mode)
        return real(self, dbname, mode)

    monkeypatch.setattr(ManagedConnection, "wal_checkpoint", checkpoint)
    time.sleep(1.05)
    state = maintenance.run_once("low")
    assert state["phase"] == "normal"
    assert modes == [0], "allocation alone must not invoke RESTART"


def test_failed_start_publication_prevents_checkpoint_and_releases_leader(small_wal, monkeypatch):
    _, maintenance, writer = small_wal
    writer.execute(
        "create trigger reject_attempt before update of maintenance on settings begin select raise(abort,'fail'); end"
    )
    modes = []
    real = ManagedConnection.wal_checkpoint

    def checkpoint(self, dbname=None, mode=0):
        modes.append(mode)
        return real(self, dbname, mode)

    monkeypatch.setattr(ManagedConnection, "wal_checkpoint", checkpoint)
    assert maintenance.run_once("startup")["last_attempt"] == "failed"
    assert modes == []
    writer.execute("drop trigger reject_attempt")
    assert maintenance.run_once("startup")["phase"] == "normal"


@pytest.mark.parametrize("race", range(5))
def test_process_barrier_serializes_policy_initialization_and_finish_cooldown(tmp_path, race):

    script = """
import json,sys
from pathlib import Path
from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.workspace import WorkspacePaths
from justpen_knowledgebase_mcp.storage.connection import SQLiteRuntime
from justpen_knowledgebase_mcp.storage.maintenance import CheckpointMaintenance
cfg=ServerConfig(workspace_dir=Path(sys.argv[1]))
with WorkspacePaths(cfg) as ws, SQLiteRuntime(ws,cfg) as factory:
    m=CheckpointMaintenance(factory)
    c=factory.connect()
    print(c.execute('select policy from settings').get,flush=True)
    for _ in range(2):
        sys.stdin.readline()
        m.run_once('startup')
        print(c.execute('select maintenance from settings').get,flush=True)
    m.close_owner()
    c.close()
"""
    peers = [
        subprocess.Popen(
            [sys.executable, "-B", "-c", script, str(tmp_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(3)
    ]
    try:
        policies = []
        for peer in peers:
            assert peer.stdout is not None
            assert peer.stderr is not None
            line = peer.stdout.readline()
            assert line, peer.stderr.read()
            policies.append(json.loads(line))
        assert policies[0] == policies[1] == policies[2]
        for peer in peers:
            assert peer.stdin is not None
            peer.stdin.write("go\n")
            peer.stdin.flush()
        first = [json.loads(peer.stdout.readline()) for peer in peers if peer.stdout is not None]
        completed = max(first, key=lambda state: state.get("sample_seq", 0))
        assert completed["sample_seq"] == 1
        # All peers start another attempt before the shared finish deadline.
        for peer in peers:
            assert peer.stdin is not None
            peer.stdin.write("go\n")
            peer.stdin.flush()
        second = [json.loads(peer.stdout.readline()) for peer in peers if peer.stdout is not None]
        assert all(state["sample_seq"] == 1 for state in second)
        assert all(state["attempt_id"] == completed["attempt_id"] for state in second)
        for peer in peers:
            _, stderr = peer.communicate(timeout=10)
            assert peer.returncode == 0, stderr
    finally:
        for peer in peers:
            if peer.poll() is None:
                peer.kill()
            peer.communicate(timeout=5)


async def test_long_passive_preserves_stale_normal_admission(kb, monkeypatch):

    entered, release = threading.Event(), threading.Event()
    real = ManagedConnection.wal_checkpoint

    def checkpoint(self, dbname=None, mode=0):
        if mode == 0:
            entered.set()
            release.wait(timeout=5)
        return real(self, dbname, mode)

    def old(c, t):
        state = json.loads(c.execute("select maintenance from settings").get)
        state.update(sample_at=time.time() - 36, next_attempt_not_before=0.0)
        c.execute("update settings set maintenance=?", (json.dumps(state),))
        return state["sample_seq"]

    seq = await kb.workers.control(old)
    monkeypatch.setattr(ManagedConnection, "wal_checkpoint", checkpoint)
    kb.maintenance.request("stale")
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for _ in range(3):
            assert await kb.workers.read(lambda c, t: c.execute("select 9").get) == 9
        snapshot = kb.maintenance.status()
        assert snapshot["stale_normal"]
        assert snapshot["sample_seq"] == seq
    finally:
        release.set()


async def test_native_restart_stall_keeps_gate_and_cached_status(kb, monkeypatch):

    entered, release = threading.Event(), threading.Event()
    real = ManagedConnection.wal_checkpoint

    def checkpoint(self, dbname=None, mode=0):
        if mode == 2:
            entered.set()
            release.wait(timeout=5)
        return real(self, dbname, mode)

    await kb.workers.control(
        lambda c, t: c.execute("update settings set maintenance=json_set(maintenance,'$.next_attempt_not_before',0.0)")
    )
    monkeypatch.setattr(ManagedConnection, "wal_checkpoint", checkpoint)
    kb.maintenance.request("pressure")
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        await asyncio.sleep(0.55)
        assert kb.maintenance.status()["phase"] == "reset"
        with pytest.raises((BusyError, LimitError)):
            await kb.workers.read(
                lambda c, t: pytest.fail("native call still owns gate"), OperationToken(time.monotonic() + 0.03)
            )
        assert kb.maintenance.status()["estimated_completion_ms"] is None
    finally:
        release.set()


def test_finish_publication_failure_never_reports_success(small_wal):
    _, maintenance, writer = small_wal
    writer.execute(
        "create trigger reject_finish before update of maintenance on settings when json_extract(new.maintenance,'$.attempt_finished_at') is not null begin select raise(abort,'fail'); end"
    )
    state = maintenance.run_once("startup")
    assert state["last_attempt"] == "failed"
    shared = json.loads(writer.execute("select maintenance from settings").get)
    assert shared["attempt_finished_at"] is None
    assert shared["sample_seq"] == 0
    assert shared["next_attempt_not_before"] > time.time()
    writer.execute("drop trigger reject_finish")
    assert maintenance.run_once("startup")["sample_seq"] == 0
    time.sleep(1.05)
    assert maintenance.run_once("startup")["sample_seq"] == 1


async def test_stale_startup_accepts_shared_normal_without_leader_or_new_sequence(kb):
    # The fixture's own checkpoint owner must not race the peer-leader probe.
    await kb.maintenance.close()

    def age(c, t):
        state = json.loads(c.execute("select maintenance from settings").get)
        state["sample_at"] = time.time() - 40
        c.execute("update settings set maintenance=?", (json.dumps(state),))
        return state["sample_seq"]

    sequence = await kb.workers.control(age)
    leader = os.open(kb.workspace.locks / "checkpoint.lock", os.O_RDWR)
    try:
        fcntl.flock(leader, fcntl.LOCK_EX | fcntl.LOCK_NB)
        async with KnowledgeBase.open(kb.config) as peer:
            assert await peer.workers.read(lambda c, t: c.execute("select 1").get) == 1
            assert peer.maintenance.status()["sample_seq"] == sequence
            assert peer.maintenance.status()["stale_normal"]
    finally:
        os.close(leader)


async def test_physical_high_guard_defers_product_even_with_normal_shared_sample(small_wal):

    factory, maintenance, writer = small_wal
    maintenance.run_once("startup")
    writer.execute("create table payload(value blob)")
    writer.execute("insert into payload values(zeroblob(131072))")
    leader = os.open(factory.workspace.locks / "checkpoint.lock", os.O_RDWR)
    fcntl.flock(leader, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        async with KnowledgeBase.open(factory.config) as peer:
            for operation in (peer.workers.read, peer.workers.write):
                with pytest.raises(WalBusyError, match="WAL_PRESSURE") as captured:
                    await operation(lambda c, t: pytest.fail("physical high must defer assessment"))
                assert captured.value.retry_after_ms >= 1000
            assert peer.maintenance.status()["phase"] == "assessment_pending"
            assert await peer.workers.control(lambda c, t: c.execute("select 1").get) == 1
    finally:
        os.close(leader)


def test_slow_publication_excludes_attempts_but_can_consume_recorded_cooldown(small_wal, monkeypatch):

    factory, peer, writer = small_wal
    peer.run_once("startup")
    writer.execute("update settings set maintenance=json_set(maintenance,'$.next_attempt_not_before',0.0)")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = CheckpointMaintenance._publish
    native_finish = []

    def publish(self, connection, state):
        if threading.current_thread().name == "slow-publication":
            native_finish.append(state.attempt_finished_at)
            entered.set()
            release.wait(timeout=5)
        original(self, connection, state)

    monkeypatch.setattr(CheckpointMaintenance, "_publish", publish)

    def attempt():
        leader = CheckpointMaintenance(factory)
        try:
            leader.run_once("timer")
        finally:
            leader.close_owner()
            finished.set()

    thread = threading.Thread(target=attempt, name="slow-publication")
    thread.start()
    try:
        assert entered.wait(5)
        before = json.loads(writer.execute("select maintenance from settings").get)
        peer.run_once("timer")
        during = json.loads(writer.execute("select maintenance from settings").get)
        assert during["attempt_id"] == before["attempt_id"]
        assert during["sample_seq"] == before["sample_seq"]
        time.sleep(1.05)
    finally:
        release.set()
        thread.join(timeout=5)
    assert finished.is_set()
    published = json.loads(writer.execute("select maintenance from settings").get)
    assert published["next_attempt_not_before"] == native_finish[0] + 1
    assert peer.run_once("timer")["sample_seq"] == published["sample_seq"] + 1


def test_crashed_attempt_keeps_start_cooldown_then_new_leader_recovers(small_wal):

    factory, maintenance, writer = small_wal
    script = """
import sys
from pathlib import Path
from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.workspace import WorkspacePaths
from justpen_knowledgebase_mcp.storage.connection import SQLiteRuntime,ManagedConnection
from justpen_knowledgebase_mcp.storage.maintenance import CheckpointMaintenance
real=ManagedConnection.wal_checkpoint
def checkpoint(self,dbname=None,mode=0):
    if mode==2:
        print('reset',flush=True)
        sys.stdin.read()
    return real(self,dbname,mode)
ManagedConnection.wal_checkpoint=checkpoint
cfg=ServerConfig(workspace_dir=Path(sys.argv[1]))
with WorkspacePaths(cfg) as ws, SQLiteRuntime(ws,cfg) as factory:
    CheckpointMaintenance(factory).run_once('startup')
"""
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", script, str(factory.workspace.root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "reset"
        process.kill()
        process.wait(timeout=5)
        state = json.loads(writer.execute("select maintenance from settings").get)
        assert state["attempt_started_at"] is not None
        assert state["sample_seq"] == 0
        assert maintenance.run_once("startup")["sample_seq"] == 0
        time.sleep(1.05)
        assert maintenance.run_once("startup")["phase"] == "normal"
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.parametrize("blocked", [False, True])
def test_low_live_frames_use_one_nonblocking_reset_and_never_pressure_on_lock_miss(small_wal, monkeypatch, blocked):

    factory, maintenance, writer = small_wal
    maintenance.run_once("startup")
    reader = factory.open_reader()
    writer.execute("create table payload(value blob)")
    writer.execute("insert into payload values(zeroblob(24576))")
    real = ManagedConnection.wal_checkpoint
    modes = []
    busy_timeouts = []

    def checkpoint(self, dbname=None, mode=0):
        modes.append(mode)
        if mode == 2:
            busy_timeouts.append(self.pragma("busy_timeout"))
        return real(self, dbname, mode)

    monkeypatch.setattr(ManagedConnection, "wal_checkpoint", checkpoint)
    time.sleep(1.05)
    try:
        with reader.gate.transaction(OperationToken(time.monotonic() + 2)) if blocked else nullcontext():
            snapshot = maintenance.run_once("low")
        assert snapshot["phase"] == "normal"
        assert snapshot["last_attempt"] == ("skipped_busy" if blocked else "restart")
        assert modes == ([0] if blocked else [0, 2])
        assert busy_timeouts == ([] if blocked else [0])
    finally:
        reader.close()


def test_failed_passive_publishes_finish_cooldown(small_wal, monkeypatch):

    _, maintenance, writer = small_wal

    def fail(self, dbname=None, mode=0):
        raise apsw.BusyError("injected checkpoint contention")

    monkeypatch.setattr(ManagedConnection, "wal_checkpoint", fail)
    snapshot = maintenance.run_once("startup")
    shared = json.loads(writer.execute("select maintenance from settings").get)
    assert shared["attempt_finished_at"] is not None
    assert shared["next_attempt_not_before"] == shared["attempt_finished_at"] + 1
    assert shared["last_attempt"] == "failed"
    assert snapshot["phase"] == "unknown"


def test_maintenance_close_failure_releases_confirmed_closed_owner_descriptors(small_wal, monkeypatch):
    _, maintenance, _ = small_wal
    maintenance.run_once("startup")
    original = ManagedConnection.close_native
    seen = []

    def fail_once(self, *, force=False):
        if self is maintenance.connection and not seen:
            seen.append(True)
            raise OSError("injected close")
        original(self, force=force)

    monkeypatch.setattr(ManagedConnection, "close_native", fail_once)
    with pytest.raises(OSError, match="injected close"):
        maintenance.close_owner()
    assert maintenance.connection is None
    assert maintenance._leader_fd is None


def test_reset_publication_busy_never_republishes_reuse_under_shared_gate(small_wal, monkeypatch):
    _, maintenance, writer = small_wal
    writer.execute("create table reset_payload(value blob)")
    writer.execute("insert into reset_payload values(zeroblob(131072))")
    publish = CheckpointMaintenance._publish
    publications = []

    def busy_once(self, connection, state):
        publications.append((state.phase, state.last_attempt, connection.gate.active_token is None))
        if len(publications) == 1:
            assert state.last_attempt == "restart"
            raise apsw.BusyError("injected first reset publication contention")
        publish(self, connection, state)

    monkeypatch.setattr(CheckpointMaintenance, "_publish", busy_once)
    snapshot = maintenance.run_once("startup")
    assert snapshot["phase"] == "unknown"
    assert publications[0] == ("normal", "restart", True)
    assert all(phase != "normal" for phase, _, _ in publications[1:])
    assert json.loads(writer.execute("select maintenance from settings").get)["last_attempt"] == "failed"
    time.sleep(1.05)
    assert maintenance.run_once("pressure")["phase"] == "normal"


@pytest.mark.parametrize("publication", ["start", "finish"])
def test_maintenance_failed_rollback_closes_inside_current_gate_and_reopens(small_wal, monkeypatch, publication):

    _, maintenance, writer = small_wal
    connection = maintenance._open()
    transaction = DbAdmissionGate.transaction
    reset_window = DbAdmissionGate.reset_window
    close_native = ManagedConnection.close_native
    helper_name = "_start_attempt" if publication == "start" else "_publish"
    helper = getattr(CheckpointMaintenance, helper_name)
    failures = []
    escaped = []
    scope_states = []
    closes = []

    def record_error(self, *args):
        try:
            return helper(self, *args)
        except apsw.Error as error:
            escaped.append(type(error))
            raise

    def record_state(gate):
        if gate is connection.gate:
            try:
                healthy = connection.get_autocommit()
            except apsw.ConnectionClosedError:
                healthy = True
            scope_states.append((gate.active, healthy))

    @contextmanager
    def watch_transaction(self, token):
        try:
            with transaction(self, token):
                yield
        finally:
            record_state(self)

    @contextmanager
    def watch_reset(self, mode):
        try:
            with reset_window(self, mode) as window:
                yield window
        finally:
            record_state(self)

    def watch_close(self, *, force=False):
        if self is connection:
            closes.append((self.gate.active, self.get_autocommit()))
        close_native(self, force=force)

    monkeypatch.setattr(
        ManagedConnection, "execute", _fail_publication_then_rollback(connection, publication, failures)
    )
    monkeypatch.setattr(ManagedConnection, "close_native", watch_close)
    monkeypatch.setattr(DbAdmissionGate, "transaction", watch_transaction)
    monkeypatch.setattr(DbAdmissionGate, "reset_window", watch_reset)
    monkeypatch.setattr(CheckpointMaintenance, helper_name, record_error)
    snapshot = maintenance.run_once("startup")
    assert failures == ["publication", "rollback"]
    assert all(healthy for _, healthy in scope_states), scope_states
    assert closes == [(True, False)]
    assert escaped == [apsw.ConstraintError]
    assert snapshot["phase"] == "unknown"
    assert connection.gate.closed
    assert maintenance.connection is None
    writer.execute("update settings set query_epoch=query_epoch+1")
    time.sleep(1.05)
    assert maintenance.run_once("startup")["phase"] == "normal"


def _fail_publication_then_rollback(connection, publication, failures):
    """Keep the real transaction open at the two precise failure boundaries."""
    execute = ManagedConnection.execute

    def fail_sql(self, sql, bindings=None, **kwargs):
        if self is connection:
            if sql.startswith("UPDATE settings SET maintenance") and not failures:
                assert bindings is not None
                state = json.loads(bindings[0])
                if (state["attempt_finished_at"] is not None) == (publication == "finish"):
                    failures.append("publication")
                    raise apsw.ConstraintError("original publication failure")
            if sql == "ROLLBACK" and failures == ["publication"]:
                failures.append("rollback")
                raise apsw.IOError("rollback failure before transaction ended")
        return execute(self, sql, bindings, **kwargs)

    return fail_sql


@pytest.mark.parametrize("phase", ["pressure", "unknown"])
async def test_startup_defers_optional_samples_until_maintenance_recovers(small_wal, phase):
    factory, maintenance, writer = small_wal
    maintenance.run_once("startup")
    state = json.loads(writer.execute("select maintenance from settings").get)
    state.update(phase=phase, next_attempt_not_before=0.0)
    writer.execute("update settings set maintenance=?", (json.dumps(state),))
    leader = os.open(factory.workspace.locks / "checkpoint.lock", os.O_RDWR)
    fcntl.flock(leader, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        async with KnowledgeBase.open(factory.config) as peer:
            status = await peer.status()
            assert not status["database"]["available"]
            assert peer.job_runner.retention_status()["stale"]
            assert not peer.job_runner.retention_status()["available"]
            assert await peer.workers.control(lambda c, _t: c.execute("select 1").get) == 1
            with pytest.raises(WalBusyError):
                await peer.workers.read(lambda _c, _t: pytest.fail("pressure bypassed"))
            fcntl.flock(leader, fcntl.LOCK_UN)
            deadline = time.monotonic() + 5
            while True:
                try:
                    assert await peer.workers.write(lambda c, _t: c.execute("select 2").get) == 2
                    break
                except WalBusyError:
                    assert time.monotonic() < deadline
                    await asyncio.sleep(0.05)
            await peer.job_runner.retention_pass(force=True)
            assert peer.job_runner.retention_status()["available"]
    finally:
        os.close(leader)
