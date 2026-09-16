"""Parse effective transport configuration before opening any sockets."""

from __future__ import annotations

import argparse
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, NoReturn

from typing_extensions import override

if TYPE_CHECKING:
    from collections.abc import Generator

    from .workspace import WorkspacePaths

from .config import ServerConfig


class _SafeArgumentParser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> NoReturn:
        del message  # argparse includes untrusted tokens in its error text.
        self.exit(2, "CONFIGURATION: invalid command syntax; use --help\n")


def parse_config(argv: list[str] | None = None) -> ServerConfig:
    """Merge explicit CLI settings before validating the effective HTTP host."""
    parser = _SafeArgumentParser(prog="justpen-knowledgebase-mcp")
    parser.add_argument("--transport", metavar="{stdio,http}")
    parser.add_argument("--host")
    parser.add_argument("--port")
    parser.add_argument("--log-level")
    return ServerConfig.from_env(os.environ, overrides=vars(parser.parse_args(argv)))


@contextmanager
def temporary_environment(workspace: WorkspacePaths) -> Generator[None]:
    """Scope process temp hints to CLI lifespan, after workspace validation.

    Explicit dirs and WorkspaceVFS remain authoritative: Python tempfile may
    already have cached a default before these environment hints are installed.
    """
    names = ("TMPDIR", "SQLITE_TMPDIR")
    previous = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ[name] = str(workspace.tmp)
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
