"""Server configuration loaded from environment variables."""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ServerConfig:
    """Runtime configuration. Loaded once at startup from env."""

    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "ServerConfig":
        """Build a config from a dict-like env mapping (typically os.environ).

        Recognized variables:
            MCP_LOG_LEVEL: standard Python log level name, defaults to ``INFO``.
        """
        log_level = env.get("MCP_LOG_LEVEL", "INFO").strip().upper()
        return cls(log_level=log_level)
