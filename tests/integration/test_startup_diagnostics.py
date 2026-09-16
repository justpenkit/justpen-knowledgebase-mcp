"""Real CLI validation and legacy layout startup preserve only safe diagnostics."""

import os
import subprocess
import sys
from contextlib import closing

import pytest

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import ConfigurationError
from justpen_knowledgebase_mcp.service import KnowledgeBase
from justpen_knowledgebase_mcp.storage.connection import SQLiteRuntime
from justpen_knowledgebase_mcp.workspace import WorkspacePaths

from .mcp_client import environment

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("settings", [{"PORT": "SECRET-MARKER"}, {"TRANSPORT": "http", "HOST": "SECRET-MARKER"}])
def test_configuration_failure_exits_without_raw_input_or_traceback(tmp_path, settings):
    env = {key: value for key, value in os.environ.items() if not key.startswith("JUSTPEN_KNOWLEDGEBASE_")}
    env["JUSTPEN_KNOWLEDGEBASE_WORKSPACE_DIR"] = str(tmp_path)
    env.update({"JUSTPEN_KNOWLEDGEBASE_" + key: value for key, value in settings.items()})
    result = subprocess.run(
        [sys.executable, "-m", "justpen_knowledgebase_mcp"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "CONFIGURATION" in result.stderr
    assert "SECRET-MARKER" not in result.stderr
    assert "Traceback" not in result.stderr


async def test_real_startup_preserves_known_offline_upgrade_reason(tmp_path):
    config = ServerConfig(workspace_dir=tmp_path)
    with (
        WorkspacePaths(config) as workspace,
        SQLiteRuntime(workspace, config) as factory,
        closing(factory.connect()) as connection,
    ):
        connection.execute("DROP INDEX jobs_active_lane")
    with pytest.raises(
        ConfigurationError, match="unsupported supporting index layout; offline workspace upgrade required"
    ):
        async with KnowledgeBase.open(config):
            pytest.fail("unsupported layout admitted startup")


def test_incompatible_layout_child_has_one_safe_configuration_line(tmp_path):
    workspace_dir = tmp_path / "SECRET-MARKER"
    workspace_dir.mkdir()
    config = ServerConfig(workspace_dir=workspace_dir)
    with (
        WorkspacePaths(config) as workspace,
        SQLiteRuntime(workspace, config) as factory,
        closing(factory.connect()) as connection,
    ):
        connection.execute("DROP INDEX jobs_active_lane")

    result = subprocess.run(
        [sys.executable, "-B", "-m", "justpen_knowledgebase_mcp"],
        env=environment(workspace_dir),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert sum(line.startswith("CONFIGURATION:") for line in result.stderr.splitlines()) == 1
    assert (
        result.stderr.splitlines().count(
            "CONFIGURATION: unsupported supporting index layout; offline workspace upgrade required"
        )
        == 1
    )
    assert "SECRET-MARKER" not in result.stderr
    assert "Traceback" not in result.stderr
