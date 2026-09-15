"""Entrypoint for `python -m justpen_knowledgebase_mcp`."""

import asyncio
import logging
import os
import signal
import sys

from .app import create_app
from .cli import parse_config, temporary_environment
from .config import ServerConfig
from .shutdown import ShutdownObserver


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

    observer = ShutdownObserver()
    mcp = create_app(config, runtime_context=temporary_environment, _shutdown_observer=observer)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}

    def request_stop() -> None:
        observer.start()
        stop_event.set()

    for sig in original_handlers:
        loop.add_signal_handler(sig, request_stop)

    if config.transport == "http":
        run_server = mcp.run_async(
            transport="http",
            host=config.host,
            port=config.port,
            path="/mcp",
            log_level=config.log_level,
            host_origin_protection=True,
            allowed_hosts=[config.host],
            uvicorn_config={"timeout_graceful_shutdown": 30, "log_level": config.log_level.lower()},
        )
    else:
        run_server = mcp.run_async()
    server_task = asyncio.create_task(run_server, name="mcp-server")
    stop_task = asyncio.create_task(stop_event.wait(), name="stop-signal")

    try:
        done, _ = await asyncio.wait({server_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done and not server_task.done():
            observer.start()
            server_task.cancel()
            # Wait for shutdown without suppressing cancellation of main itself.
            await _await_cleanup(server_task)
            if server_task.cancelled():
                return
        await server_task
    finally:
        observer.start()
        server_task.cancel()
        stop_task.cancel()
        try:
            await _await_cleanup(server_task)
        finally:
            try:
                await asyncio.gather(stop_task, return_exceptions=True)
                await observer.close()
            finally:
                for sig, handler in original_handlers.items():
                    loop.remove_signal_handler(sig)
                    signal.signal(sig, handler)


async def _await_cleanup(task: asyncio.Task[None]) -> None:
    # The shared observer covers both transport teardown and native owner close.
    cancelled = False
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            cancelled = True
    if not task.cancelled():
        task.exception()
    if cancelled:
        raise asyncio.CancelledError


def cli() -> None:
    """Sync entrypoint for the ``justpen-knowledgebase-mcp`` console script."""
    asyncio.run(main(parse_config()))


if __name__ == "__main__":
    cli()
