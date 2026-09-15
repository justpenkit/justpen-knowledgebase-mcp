"""Real owner-thread workers, queues, cancellation and transaction guards."""

import asyncio
import subprocess
import sys
import threading
import time

import pytest

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import (
    BusyError,
    CancelledOperationError,
    ConfigurationError,
    InternalError,
    LimitError,
)
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.worker import OperationToken

pytestmark = pytest.mark.integration


async def test_readers_do_not_block_writer_or_control(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        entered = threading.Barrier(3)
        release = threading.Event()

        def hold(connection, token):
            entered.wait(timeout=5)
            release.wait(timeout=5)
            return connection.execute("select 7").get

        reads = [asyncio.create_task(kb.workers.read(hold)) for _ in range(2)]
        try:
            await asyncio.to_thread(entered.wait, 5)
            assert await kb.workers.write(lambda c, t: c.execute("select 8").get) == 8
            assert await kb.workers.control(lambda c, t: c.execute("select 9").get) == 9
        finally:
            release.set()
        assert await asyncio.gather(*reads) == [7, 7]


async def test_precommit_cancel_rolls_back_then_next_write_runs(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        entered, release = threading.Event(), threading.Event()
        token = OperationToken(time.monotonic() + 5)

        def change(connection, token):
            connection.execute("insert into nodes(uuid,type,key,properties) values ('one','ip','a','{}')")
            entered.set()
            release.wait(timeout=5)

        task = asyncio.create_task(kb.workers.write(change, token))
        await asyncio.to_thread(entered.wait, 5)
        assert kb.workers.interrupt_if_current(token)
        release.set()
        with pytest.raises(CancelledOperationError):
            await task
        assert await kb.workers.write(lambda c, t: c.execute("select count(*) from nodes").get) == 0


@pytest.mark.parametrize("field", ["schema_version", "catalog_version", "catalog_fingerprint", "index_format_version"])
async def test_guard_rejects_stale_process_operations(tmp_path, field):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path)) as kb:
        probe = """
import sys
from pathlib import Path
from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.workspace import WorkspacePaths
from justpen_knowledgebase_mcp.storage.connection import SQLiteRuntime
config=ServerConfig(workspace_dir=Path(sys.argv[1]))
with WorkspacePaths(config) as workspace, SQLiteRuntime(workspace,config) as runtime:
    connection=runtime.connect()
    try:
        field=sys.argv[2]
        assert field in ('schema_version','catalog_version','catalog_fingerprint','index_format_version')
        connection.execute('UPDATE settings SET '+field+'=?, query_epoch=query_epoch+1',('changed' if field=='catalog_fingerprint' else 2,))
    finally:
        connection.close()
"""
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-B", "-c", probe, str(tmp_path), field],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        for operation in (kb.workers.read, kb.workers.write, kb.workers.control):
            with pytest.raises(ConfigurationError):
                await operation(lambda c, t: c.execute("select 1").get)


async def test_queue_deadline_is_limit_and_does_not_run_callback(kb):
    entered, release = threading.Event(), threading.Event()

    def hold(c, t):
        entered.set()
        release.wait(timeout=5)

    running = asyncio.create_task(kb.workers.write(hold))
    await asyncio.to_thread(entered.wait, 5)
    called = threading.Event()
    try:
        with pytest.raises(LimitError):
            await kb.workers.write(lambda c, t: called.set(), OperationToken(time.monotonic() + 0.03))
    finally:
        release.set()
        await running
    await kb.workers.write(lambda c, t: None)
    assert not called.is_set()


@pytest.mark.parametrize(("lane", "capacity"), [("write", 128), ("control", 16), ("read", 128)])
async def test_saturation_is_bounded(kb, lane, capacity):
    entered = threading.Barrier(3 if lane == "read" else 2)
    release = threading.Event()

    def hold(c, t):
        entered.wait(timeout=5)
        release.wait(timeout=5)

    operation = getattr(kb.workers, lane)
    running = [asyncio.create_task(operation(hold)) for _ in range(2 if lane == "read" else 1)]
    await asyncio.to_thread(entered.wait, 5)
    queued = [asyncio.create_task(operation(lambda c, t: 1)) for _ in range(capacity)]
    try:
        await asyncio.sleep(0)
        with pytest.raises(BusyError):
            await operation(lambda c, t: 2)
        assert len(kb.workers._queues[lane]) == capacity
    finally:
        release.set()
        await asyncio.gather(*running, *queued)


async def test_control_fairness(kb):
    entered, release = threading.Event(), threading.Event()

    def hold(c, t):
        entered.set()
        release.wait(timeout=5)

    running = asyncio.create_task(kb.workers.write(hold))
    await asyncio.to_thread(entered.wait, 5)
    order = []
    queued = [asyncio.create_task(kb.workers.control(lambda c, t: order.append("control"))) for _ in range(9)]
    queued.append(asyncio.create_task(kb.workers.write(lambda c, t: order.append("normal"))))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(running, *queued)
    assert order[:5] == ["control"] * 4 + ["normal"]


@pytest.mark.parametrize("lane", ["read", "write"])
async def test_interrupt_running_sql_does_not_affect_next_generation(kb, lane):
    entered = threading.Event()
    token = OperationToken(time.monotonic() + 5)

    def query(c, t):
        entered.set()
        return c.execute(
            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n"
        ).get

    operation = getattr(kb.workers, lane)
    task = asyncio.create_task(operation(query, token))
    await asyncio.to_thread(entered.wait, 5)
    assert kb.workers.interrupt_if_current(token)
    with pytest.raises(CancelledOperationError):
        await task
    assert not kb.workers.interrupt_if_current(token)
    assert await operation(lambda c, t: c.execute("select 1").get) == 1


@pytest.mark.parametrize("mutation", ["insert", "update"])
async def test_insert_interrupt_autorollback_preserves_original_error(kb, mutation):
    entered = threading.Event()
    token = OperationToken(time.monotonic() + 5)
    autocommit = []
    if mutation == "update":
        await kb.workers.write(
            lambda c, t: c.execute("insert into nodes(uuid,type,key,properties) values ('existing','ip','a','{}')")
        )

    def insert(c, t):
        def started(x):
            entered.set()
            return x

        c.create_scalar_function("started", started, 1)
        try:
            if mutation == "insert":
                c.execute(
                    "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000000) INSERT INTO nodes(uuid,type,key,properties) SELECT cast(started(x) as text),'ip',cast(x as text),'{}' FROM n"
                )
            else:
                c.execute(
                    "UPDATE nodes SET key=(WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(started(x)) FROM n)"
                )
        finally:
            autocommit.append(c.get_autocommit())

    task = asyncio.create_task(kb.workers.write(insert, token))
    await asyncio.to_thread(entered.wait, 5)
    kb.workers.interrupt_if_current(token)
    with pytest.raises(CancelledOperationError):
        await task
    assert autocommit == [True]
    expected = 0 if mutation == "insert" else 1
    assert await kb.workers.write(lambda c, t: c.execute("select count(*) from nodes").get) == expected


async def test_cancel_after_commit_boundary_returns_committed_result(kb):
    entered, release = threading.Event(), threading.Event()
    token = OperationToken(time.monotonic() + 5)

    def change(c, t):
        def commit_hook():
            entered.set()
            release.wait(timeout=5)
            return False

        c.set_commit_hook(commit_hook)
        c.execute("insert into nodes(uuid,type,key,properties) values ('a','ip','a','{}')")
        return 7

    task = asyncio.create_task(kb.workers.write(change, token))
    await asyncio.to_thread(entered.wait, 5)
    try:
        assert token.state == "committing"
        task.cancel()
        await asyncio.sleep(0)
        assert not kb.workers.interrupt_if_current(token)
    finally:
        release.set()
    assert await task == 7
    assert await kb.workers.read(lambda c, t: c.execute("select count(*) from nodes").get) == 1


async def test_two_services_close_independently(tmp_path):
    config = ServerConfig(workspace_dir=tmp_path)
    async with KnowledgeBase.open(config) as first:
        async with KnowledgeBase.open(config) as second:
            assert await second.workers.read(lambda c, t: c.execute("select 1").get) == 1
        assert await first.workers.read(lambda c, t: c.execute("select 2").get) == 2


async def test_repeated_cancel_after_commit_boundary_still_returns_result(kb):
    entered, release = threading.Event(), threading.Event()
    token = OperationToken(time.monotonic() + 5)

    def change(c, t):
        def commit_hook():
            entered.set()
            release.wait(timeout=5)
            return False

        c.set_commit_hook(commit_hook)
        c.execute("insert into nodes(uuid,type,key,properties) values ('a','ip','a','{}')")
        return 8

    task = asyncio.create_task(kb.workers.write(change, token))
    await asyncio.to_thread(entered.wait, 5)
    try:
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
    assert await task == 8


async def test_busy_wait_uses_remaining_operation_budget(kb):
    blocker = kb.workers.factory.connect()
    blocker.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(LimitError):
            await kb.workers.write(lambda c, t: None, OperationToken(started + 0.05))
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert time.monotonic() - started < 0.5
    assert await kb.workers.write(lambda c, t: c.execute("select 1").get) == 1


async def test_shutdown_cancels_running_and_queued_and_stops_admission(kb):
    entered, release = threading.Event(), threading.Event()

    def hold(c, t):
        entered.set()
        release.wait(timeout=5)

    running = asyncio.create_task(kb.workers.write(hold))
    await asyncio.to_thread(entered.wait, 5)
    queued = asyncio.create_task(kb.workers.write(lambda c, t: 1))
    await asyncio.sleep(0)
    shutdown = asyncio.create_task(kb.workers.close())
    await asyncio.sleep(0)
    try:
        with pytest.raises(BusyError):
            await kb.workers.write(lambda c, t: 1)
    finally:
        release.set()
    for task in (running, queued):
        with pytest.raises(CancelledOperationError):
            await task
    await shutdown
    assert all(owner.connection is None for owner in kb.workers._owners)


async def test_unexpected_callback_failure_is_sanitized_and_rolled_back(kb):
    def fail(c, t):
        c.execute("insert into nodes(uuid,type,key,properties) values ('a','ip','a','{}')")
        raise KeyError("/private/secret path and evidence content")

    with pytest.raises(InternalError, match=r"^database operation failed$"):
        await kb.workers.write(fail)
    assert await kb.workers.read(lambda c, t: c.execute("select count(*) from nodes").get) == 0


async def test_eight_readers_still_share_one_queue_of_128(tmp_path):
    async with KnowledgeBase.open(ServerConfig(workspace_dir=tmp_path, db_reader_threads=8)) as kb:
        entered = threading.Barrier(9)
        release = threading.Event()

        def hold(c, t):
            entered.wait(timeout=5)
            release.wait(timeout=5)

        running = [asyncio.create_task(kb.workers.read(hold)) for _ in range(8)]
        await asyncio.to_thread(entered.wait, 5)
        queued = [asyncio.create_task(kb.workers.read(lambda c, t: 1)) for _ in range(128)]
        try:
            await asyncio.sleep(0)
            with pytest.raises(BusyError):
                await kb.workers.read(lambda c, t: 2)
        finally:
            release.set()
            await asyncio.gather(*running, *queued)
