"""Shutdown diagnostics contain process identity without cancelling cleanup."""

import os

from justpen_knowledgebase_mcp.shutdown import ShutdownObserver


async def test_shutdown_timeout_names_process_and_supervisor_action(caplog):
    await ShutdownObserver()._observe(0)
    assert f"pid={os.getpid()}" in caplog.text
    assert "supervisor must kill" in caplog.text
