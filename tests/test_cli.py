"""CLI overrides and process-only temporary environment ownership."""

import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from justpen_knowledgebase_mcp import __main__ as entrypoint
from justpen_knowledgebase_mcp.cli import parse_config, temporary_environment
from justpen_knowledgebase_mcp.workspace import WorkspacePaths


def test_cli_overrides_env(monkeypatch):
    monkeypatch.setenv("JUSTPEN_KNOWLEDGEBASE_WORKSPACE_DIR", "/workspace")
    monkeypatch.setenv("JUSTPEN_KNOWLEDGEBASE_TRANSPORT", "http")
    monkeypatch.setenv("JUSTPEN_KNOWLEDGEBASE_HOST", "192.0.2.1")
    config = parse_config(["--host", "localhost", "--port", "1234", "--log-level", "DEBUG"])
    assert (config.host, config.port, config.log_level) == ("127.0.0.1", 1234, "DEBUG")


@pytest.mark.parametrize("spelling", ["debug", "DEBUG", "Debug"])
def test_cli_log_level_accepts_same_case_spellings_as_environment(monkeypatch, spelling):
    monkeypatch.setenv("JUSTPEN_KNOWLEDGEBASE_WORKSPACE_DIR", "/workspace")
    config = parse_config(["--log-level", spelling])
    assert config.log_level == "DEBUG"


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


@pytest.mark.parametrize(
    "args",
    [
        ["--port", "SECRET-MARKER"],
        ["--transport", "SECRET-MARKER"],
        ["--SECRET-MARKER"],
        ["--port=SECRET-MARKER", "--host"],
    ],
)
def test_cli_invalid_values_and_syntax_are_sanitized(monkeypatch, capsys, args):
    monkeypatch.setenv("JUSTPEN_KNOWLEDGEBASE_WORKSPACE_DIR", "/workspace")
    monkeypatch.setattr("sys.argv", ["kb", *args])
    with pytest.raises(SystemExit) as raised:
        entrypoint.cli()
    assert raised.value.code != 0
    stderr = capsys.readouterr().err
    assert "SECRET-MARKER" not in stderr
    assert "Traceback" not in stderr
    assert "CONFIGURATION" in stderr


def test_cli_help_still_exits_successfully(capsys):
    with pytest.raises(SystemExit) as raised:
        parse_config(["--help"])
    assert raised.value.code == 0
    assert "--transport" in capsys.readouterr().out
