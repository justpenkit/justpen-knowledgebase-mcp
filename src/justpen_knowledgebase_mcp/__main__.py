"""Entrypoint for `python -m justpen_knowledgebase_mcp`."""

import argparse
import asyncio
import logging
import os
import signal
import sys

from .app import mcp
from .config import ServerConfig
from .tools import register_all


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )


async def main(config: ServerConfig | None = None) -> None:
    """Launch the MCP server on stdio with graceful shutdown on SIGTERM/SIGINT."""
    config = config or ServerConfig.from_env(os.environ)
    _setup_logging(config.log_level)

    register_all(mcp)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    if config.transport == "http":
        run_server = mcp.run_async(transport="http", host=config.host, port=config.port, path="/mcp")
    else:
        run_server = mcp.run_async()
    server_task = asyncio.create_task(run_server, name="mcp-server")
    stop_task = asyncio.create_task(stop_event.wait(), name="stop-signal")

    try:
        done, _ = await asyncio.wait({server_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done and not server_task.done():
            server_task.cancel()
            # Wait for shutdown without suppressing cancellation of main itself.
            await asyncio.wait({server_task})
            if server_task.cancelled():
                return
        await server_task
    finally:
        server_task.cancel()
        stop_task.cancel()
        await asyncio.gather(server_task, stop_task, return_exceptions=True)


def parse_config(argv: list[str] | None = None) -> ServerConfig:
    """Merge explicit CLI settings before validating the effective HTTP host."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=("stdio", "http"))
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--log-level")
    return ServerConfig.from_env(os.environ, overrides=vars(parser.parse_args(argv)))


def cli() -> None:
    """Sync entrypoint for the ``justpen-knowledgebase-mcp`` console script."""
    asyncio.run(main(parse_config()))


if __name__ == "__main__":
    cli()
