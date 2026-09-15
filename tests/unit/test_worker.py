"""Owner worker coordination with isolated managed connections and deterministic events."""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import apsw
import pytest

from justpen_knowledgebase_mcp.errors import (
    BusyError,
    CancelledOperationError,
    InternalError,
    LimitError,
    StorageIOError,
)
from justpen_knowledgebase_mcp.storage import worker


def factory():
    def connection():
        db = Mock(retired=False)
        db.rollback_or_retire.return_value = None
        db.gate.transaction.side_effect = lambda _token: nullcontext()
        return db

    result = Mock(config=SimpleNamespace(db_reader_threads=1, query_timeout_ms=10000, db_busy_timeout_ms=10))
    result.open_reader.side_effect = connection
    result.open_writer.side_effect = connection
    result.status_cache.snapshot.return_value = {"retry_after_ms": 123}
    return result


async def test_owner_threads_commit_rollback_control_and_close():
    runtime = factory()
    workers = worker.DatabaseWorkers(runtime)
    await workers.start()
    try:
        token = worker.OperationToken(float("inf"))
        assert await workers.write(lambda _c, _t: {"written": 2}, token) == {"written": 2}
        assert token.state == "done"
        assert token.wal_retry_after_ms == 123
        assert await workers.read(lambda _c, _t: 3) == 3
        runtime.check_product.reset_mock()
        assert await workers.control(lambda _c, _t: 4) == 4
        runtime.check_product.assert_not_called()
        runtime.committed.assert_called_once()
        with pytest.raises(ValueError, match="already submitted"):
            await workers.write(lambda _c, _t: None, token)

        def fail(_connection, _token):
            raise ValueError("secret row")

        with pytest.raises(InternalError, match="database operation failed"):
            await workers.read(fail)
        assert await workers.read(lambda _c, _t: 5) == 5
        assert workers.queue_status()["read_queued"] == 0
    finally:
        await workers.close()
    assert runtime.close_connection.call_count == 2
    with pytest.raises(BusyError):
        await workers.read(lambda _c, _t: None)


async def test_startup_failure_unwinds_and_stops_admission():
    runtime = factory()
    runtime.open_writer.side_effect = OSError("private path")
    workers = worker.DatabaseWorkers(runtime)
    with pytest.raises(OSError):
        await workers.start()
    await workers.close()
    assert workers.queue_status()["stopping"]


async def test_queue_cancellation_removes_work_before_callback():
    workers = worker.DatabaseWorkers(factory())
    callback = Mock()
    token = worker.OperationToken(float("inf"))
    task = asyncio.create_task(workers.read(callback, token))
    await asyncio.sleep(0)
    assert workers.queue_status()["read_queued"] == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert token.cancelled
    workers._loop = asyncio.get_running_loop()
    workers._fail_queued()
    await asyncio.sleep(0)
    callback.assert_not_called()
    assert token.state == "done"


async def test_control_fairness_and_generation_safe_interrupt():
    workers = worker.DatabaseWorkers(factory())
    loop = asyncio.get_running_loop()
    connection = Mock()
    owner = worker._Owner(reader=False, ready=loop.create_future(), closed=loop.create_future(), connection=connection)
    workers._owners = [owner]
    controls = [
        worker._Work(Mock(), worker.OperationToken(float("inf")), loop.create_future(), "control") for _ in range(5)
    ]
    write = worker._Work(Mock(), worker.OperationToken(float("inf")), loop.create_future(), "write")
    workers._queues["control"].extend(controls)
    workers._queues["write"].append(write)
    streak = 0
    for expected in [*controls[:4], write, controls[4]]:
        selected, streak = workers._next(owner, streak)
        assert selected is expected
    old = controls[0].token
    assert workers.interrupt_if_current(old)
    connection.interrupt.assert_not_called()
    assert workers.interrupt_if_current(controls[-1].token)
    connection.interrupt.assert_called_once()
    controls[-1].token.state = "committing"
    assert not workers.interrupt_if_current(controls[-1].token)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (apsw.BusyError("secret"), BusyError),
        (apsw.IOError("secret"), StorageIOError),
        (apsw.FullError("secret"), StorageIOError),
        (OSError("secret"), StorageIOError),
        (ValueError("secret"), InternalError),
    ],
)
def test_public_errors_sanitize_native_details(error, expected):
    result = worker._public_error(error, worker.OperationToken(float("inf")))
    assert isinstance(result, expected)
    assert "secret" not in str(result)
    assert isinstance(worker._public_error(error, worker.OperationToken(0)), LimitError)
    assert isinstance(
        worker._public_error(error, worker.OperationToken(float("inf"), cancelled=True)), CancelledOperationError
    )


async def test_committed_result_survives_repeated_cancellation():
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    task = asyncio.create_task(worker._committed_result(future))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    future.set_result("committed")
    assert await task == "committed"


async def test_failed_cleanup_stops_owner_before_any_connection_reuse():
    runtime = factory()
    db = Mock(retired=True)
    db.gate.transaction.side_effect = lambda _token: nullcontext()
    db.rollback_or_retire.return_value = OSError("rollback failed")
    runtime.open_writer.side_effect = lambda: db
    workers = worker.DatabaseWorkers(runtime)
    await workers.start()
    try:
        # The committed result remains true; retirement separately stops admission.
        assert await workers.write(lambda _c, _t: "committed") == "committed"
    finally:
        await workers.close()
    assert workers.queue_status()["stopping"]
    assert [call.args[0] for call in db.execute.call_args_list] == ["BEGIN IMMEDIATE", "COMMIT"]
    callback = Mock()
    with pytest.raises(BusyError):
        await workers.write(callback)
    callback.assert_not_called()


def test_operation_token_prioritizes_cancellation_and_expires():
    with pytest.raises(CancelledOperationError):
        worker.OperationToken(0, cancelled=True).check()
    with pytest.raises(LimitError):
        worker.OperationToken(0).check()
