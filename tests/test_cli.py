"""CLI overrides and process-only temporary environment ownership."""

import os
from pathlib import Path
from unittest.mock import Mock

from justpen_knowledgebase_mcp.cli import parse_config, temporary_environment
from justpen_knowledgebase_mcp.workspace import WorkspacePaths


def test_cli_overrides_env(monkeypatch):
    monkeypatch.setenv("JUSTPEN_KNOWLEDGEBASE_WORKSPACE_DIR", "/workspace")
    monkeypatch.setenv("JUSTPEN_KNOWLEDGEBASE_TRANSPORT", "http")
    monkeypatch.setenv("JUSTPEN_KNOWLEDGEBASE_HOST", "192.0.2.1")
    config = parse_config(["--host", "localhost", "--port", "1234", "--log-level", "DEBUG"])
    assert (config.host, config.port, config.log_level) == ("127.0.0.1", 1234, "DEBUG")


def test_cli_temp_context_restores_environment(monkeypatch):
    monkeypatch.setenv("TMPDIR", "original")
    monkeypatch.delenv("SQLITE_TMPDIR", raising=False)
    workspace = Mock(spec=WorkspacePaths)
    workspace.tmp = Path("/workspace/managed/tmp")
    with temporary_environment(workspace):
        assert os.environ["TMPDIR"] == "/workspace/managed/tmp"
        assert os.environ["SQLITE_TMPDIR"] == "/workspace/managed/tmp"
    assert os.environ["TMPDIR"] == "original"
    assert "SQLITE_TMPDIR" not in os.environ
