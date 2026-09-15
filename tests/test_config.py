"""Immutable startup configuration and effective binding validation."""

import pytest
from pydantic import ValidationError

from justpen_knowledgebase_mcp import __main__ as entrypoint
from justpen_knowledgebase_mcp.config import ServerConfig

PREFIX = "JUSTPEN_KNOWLEDGEBASE_"


def test_workspace_is_mandatory():
    with pytest.raises(ValidationError):
        ServerConfig.from_env({})


def test_relative_workspace_is_rejected():
    with pytest.raises(ValidationError):
        ServerConfig.from_env({PREFIX + "WORKSPACE_DIR": "relative"})


def test_prefix_defaults_and_immutability():
    cfg = ServerConfig.from_env({PREFIX + "WORKSPACE_DIR": "/workspace", "MCP_LOG_LEVEL": "DEBUG"})
    assert cfg.log_level == "INFO"
    assert (cfg.transport, cfg.host, cfg.port) == ("stdio", "127.0.0.1", 8934)
    assert (cfg.db_busy_timeout_ms, cfg.query_timeout_ms, cfg.db_reader_threads) == (5000, 10000, 2)
    with pytest.raises(ValidationError):
        cfg.log_level = "DEBUG"


@pytest.mark.parametrize("host", ["127.0.0.1", "127.9.8.7", "::1", "localhost"])
def test_loopback_http(host):
    cfg = ServerConfig.from_env(
        {PREFIX + "WORKSPACE_DIR": "/workspace", PREFIX + "TRANSPORT": "http", PREFIX + "HOST": host}
    )
    assert cfg.host == ("127.0.0.1" if host == "localhost" else host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "example.com", "127.0.0.1.example.com", "192.0.2.1"])  # noqa: S104 - verify wildcard rejection without binding
def test_non_loopback_requires_explicit_opt_in_after_cli_override(host):
    env = {PREFIX + "WORKSPACE_DIR": "/workspace"}
    with pytest.raises(ValidationError):
        ServerConfig.from_env(env, overrides={"transport": "http", "host": host})
    env[PREFIX + "ALLOW_NON_LOOPBACK"] = "true"
    assert ServerConfig.from_env(env, overrides={"transport": "http", "host": host}).host == host


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("DB_READER_THREADS", "0"),
        ("DB_READER_THREADS", "9"),
        ("PORT", "0"),
        ("QUERY_TIMEOUT_MS", "0"),
        ("ALLOW_NON_LOOPBACK", "yes"),
        ("TRANSPORT", "sse"),
        ("LOG_LEVEL", "bad"),
    ],
)
def test_invalid_settings(key, value):
    with pytest.raises(ValidationError):
        ServerConfig.from_env({PREFIX + "WORKSPACE_DIR": "/workspace", PREFIX + key: value})


def test_cli_override_guard_applies_before_server_launch(monkeypatch):
    monkeypatch.setenv(PREFIX + "WORKSPACE_DIR", "/workspace")
    with pytest.raises(ValidationError):
        entrypoint.parse_config(["--transport", "http", "--host", "192.0.2.1"])
