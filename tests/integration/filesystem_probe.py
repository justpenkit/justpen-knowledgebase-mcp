"""Cold interpreter native audit workload; stdout belongs to the harness report."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import apsw

from justpen_knowledgebase_mcp.__main__ import cli
from justpen_knowledgebase_mcp.cli import temporary_environment
from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import StorageIOError
from justpen_knowledgebase_mcp.models import DeleteRequest
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.jobs import JobStore
from justpen_knowledgebase_mcp.telemetry.config import read_config
from justpen_knowledgebase_mcp.telemetry.runtime import initialize


async def unavailable_tmp(kb, scenario):
    if scenario not in {"removed-tmp", "unwritable-tmp"}:
        return
    if scenario == "removed-tmp":
        kb.workspace.tmp.rmdir()
    else:
        kb.workspace.tmp.chmod(0o500)
    try:
        try:
            await kb.workers.control(lambda c, _t: list(c.execute("select value from spill order by value")))
        except (StorageIOError, apsw.IOError):
            pass
        else:
            raise AssertionError("native spill used a fallback after TMP became unavailable")
    finally:
        if scenario == "unwritable-tmp":
            kb.workspace.tmp.chmod(0o700)


def escaped_control(root, outside):
    escaped = root / ".." / outside.name
    denied = 0
    for operation in [
        (escaped / "victim").unlink,
        lambda: (root / "stage").rename(escaped / "renamed"),
        (escaped / "created").mkdir,
    ]:
        try:
            operation()
        except PermissionError:
            denied += 1
    print(json.dumps({"denied_operations": denied}))


def denied_control(outside):
    try:
        (outside / "forbidden").write_bytes(b"must fail")
    except PermissionError:
        print(json.dumps({"denied": True}))
        return
    raise AssertionError("native policy failed to deny external write")


def main():
    root, outside, scenario = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    # This import intentionally occurs after process startup under the native policy.

    assert callable(cli)
    if scenario == "denied-control":
        denied_control(outside)
        return

    if scenario == "escaped-control":
        escaped_control(root, outside)
        return

    telemetry = None
    if scenario.startswith("telemetry"):
        telemetry = initialize(read_config(os.environ), service_version="audit")

    async def workload():
        recovery_id = str(uuid4())
        async with KnowledgeBase.open(ServerConfig(workspace_dir=root), runtime_context=temporary_environment) as kb:
            result = await kb.ingest_evidence({"text": "native cold import evidence token"})
            assert result["state"] == "completed", result
            evidence = await kb.read_evidence({"evidence_id": result["evidence_id"]})
            assert "token" in evidence["content"]

            rows = 4000 if sys.platform.startswith("linux") else 30000

            def spill(connection, _token):
                connection.pragma("cache_size", -64)
                connection.execute("create table spill(value)")
                connection.execute(
                    "with recursive n(x) as (values(1) union all select x+1 from n where x<?) "
                    "insert into spill select randomblob(2048) from n",
                    (rows,),
                )
                assert sum(1 for _ in connection.execute("select value from spill order by value")) == rows

            await kb.workers.control(spill)
            assert kb.workers.factory.vfs.temp_open_count > 0
            await unavailable_tmp(kb, scenario)
            report = {
                "evidence_id": result["evidence_id"],
                "native_spills": kb.workers.factory.vfs.temp_open_count,
                "spill_input_bytes": rows * 2048,
            }
            if scenario == "recovery":
                await kb.job_runner.close()
                await kb.workers.control(
                    lambda c, _t: JobStore.admit_delete(
                        c, DeleteRequest(kind="evidence", ids=[result["evidence_id"]], cascade=True), recovery_id
                    )
                )
                await kb.workers.control(
                    lambda c, _t: c.execute("delete from jobs where uuid=?", (recovery_id,)).fetchall()
                )
        if scenario == "recovery":
            async with KnowledgeBase.open(
                ServerConfig(workspace_dir=root), runtime_context=temporary_environment
            ) as kb:
                recovered = await kb.job_runner.wait(recovery_id, time.monotonic() + 10)
                assert recovered["state"] == "completed", recovered
                assert (await kb.get({"kind": "evidence", "ids": [result["evidence_id"]]}))["missing_ids"] == [
                    result["evidence_id"]
                ]
                assert not list(kb.workspace.evidence.rglob(result["evidence_id"][2:]))
        return report

    async def run():
        try:
            return await workload()
        finally:
            if telemetry is not None:
                await telemetry.shutdown()

    print(json.dumps(asyncio.run(run())))
    assert not list(root.rglob("__pycache__"))
    assert not (outside / "forbidden").exists()
    assert os.environ.get("PYTHONDONTWRITEBYTECODE") == "1"


if __name__ == "__main__":
    main()
