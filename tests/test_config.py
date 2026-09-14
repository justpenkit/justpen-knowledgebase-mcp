"""Tests for ServerConfig.from_env."""

from dataclasses import FrozenInstanceError

from justpen_knowledgebase_mcp.config import ServerConfig


def test_defaults_when_env_empty():
    cfg = ServerConfig.from_env({})
    assert cfg.log_level == "INFO"


def test_log_level_from_env_is_upper_stripped():
    cfg = ServerConfig.from_env({"MCP_LOG_LEVEL": "  debug  "})
    assert cfg.log_level == "DEBUG"


def test_server_config_is_frozen():
    cfg = ServerConfig.from_env({})
    try:
        cfg.log_level = "DEBUG"  # type: ignore[misc]
    except FrozenInstanceError:
        return
    raise AssertionError("ServerConfig should be frozen")
