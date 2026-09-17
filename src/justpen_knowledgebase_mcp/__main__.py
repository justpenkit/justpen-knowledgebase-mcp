"""Entrypoint for `python -m justpen_knowledgebase_mcp`."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from importlib.metadata import version
from typing import TYPE_CHECKING

import anyio

from .cli import parse_config, temporary_environment
from .config import ServerConfig
from .errors import ConfigurationError, McpError, PathDeniedError
from .shutdown import ShutdownObserver
from .telemetry.config import TelemetryConfig, configure_sdk_environment, read_config

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from contextlib import AbstractContextManager
    from types import FrameType

    from fastmcp import FastMCP

    from .telemetry.runtime import TelemetryRuntime
    from .workspace import WorkspacePaths


def create_app(
    config: ServerConfig,
    *,
    runtime_context: Callable[[WorkspacePaths], AbstractContextManager[None]] | None = None,
    _shutdown_observer: ShutdownObserver | None = None,
    telemetry: TelemetryRuntime | None = None,
) -> FastMCP:
    """Import native instrumentation only after the CLI isolates OTel settings."""
    from .app import create_app as factory  # noqa: PLC0415 — SDK resolves providers at import time

    return factory(config, runtime_context=runtime_context, _shutdown_observer=_shutdown_observer, telemetry=telemetry)


def _initialize_telemetry(config: TelemetryConfig, *, service_version: str) -> TelemetryRuntime:
    configure_sdk_environment(config)
    from .telemetry.export import protect_cli_diagnostics  # noqa: PLC0415 — SDK imports follow environment isolation
    from .telemetry.runtime import initialize  # noqa: PLC0415 — ambient providers must be removed first

    protect_cli_diagnostics()
    return initialize(config, service_version=service_version)


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

    telemetry = _initialize_telemetry(read_config(os.environ), service_version=version("justpen-knowledgebase-mcp"))
    logging.getLogger("fastmcp").setLevel(config.log_level)
    try:
        await _serve(config, telemetry)
    except BaseException:
        telemetry.events.lifecycle("mcp.server.failed", {})
        raise
    finally:
        telemetry.events.lifecycle("mcp.server.stopped", {})
        await telemetry.shutdown()


async def _serve(config: ServerConfig, telemetry: TelemetryRuntime) -> None:
    observer = ShutdownObserver()
    mcp = create_app(config, runtime_context=temporary_environment, _shutdown_observer=observer, telemetry=telemetry)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}

    def request_stop() -> None:
        observer.start()
        stop_event.set()

    installed: list[signal.Signals] = []
    server_task: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[bool] | None = None
    cancel_server: Callable[[], object] | None = None

    try:
        for sig in original_handlers:
            loop.add_signal_handler(sig, request_stop)
            installed.append(sig)
        server_task, cancel_server = _start_server(mcp, config, telemetry)
        stop_task = asyncio.create_task(stop_event.wait(), name="stop-signal")
        done, _ = await asyncio.wait({server_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done and not server_task.done():
            observer.start()
            cancel_server()
            # Native owners drain before optional exporters are closed by main.
            await _await_cleanup(server_task)
            if server_task.cancelled():
                return
        await server_task
    finally:
        observer.start()
        if cancel_server is not None:
            cancel_server()
        if stop_task is not None:
            stop_task.cancel()
        try:
            if server_task is not None:
                await _await_cleanup(server_task)
        finally:
            try:
                if stop_task is not None:
                    await asyncio.gather(stop_task, return_exceptions=True)
                await observer.close()
            finally:
                _restore_handlers(loop, installed, original_handlers)


def _start_server(
    mcp: FastMCP, config: ServerConfig, telemetry: TelemetryRuntime
) -> tuple[asyncio.Task[None], Callable[[], object]]:
    if config.transport == "http":
        task = asyncio.create_task(
            mcp.run_async(
                transport="http",
                host=config.host,
                port=config.port,
                path="/mcp",
                log_level=None,
                middleware=telemetry.asgi_middleware(),
                host_origin_protection=True,
                allowed_hosts=[config.host, *config.allowed_hosts],
                uvicorn_config={
                    "timeout_graceful_shutdown": 30,
                    "log_level": config.log_level.lower(),
                    "log_config": None,
                    "access_log": False,
                },
            ),
            name="mcp-server",
        )
        return task, task.cancel
    scope = anyio.CancelScope()
    # Cancel SDK relays with their host, before it closes their channels.
    return asyncio.create_task(_run_stdio(mcp, scope), name="mcp-server"), scope.cancel


async def _run_stdio(mcp: FastMCP, scope: anyio.CancelScope) -> None:
    # The server task owns scope entry/exit, including a stop requested before
    # this coroutine starts. HTTP retains its existing task-cancellation path.
    with scope:
        await mcp.run_async(transport="stdio")


def _restore_handlers(
    loop: asyncio.AbstractEventLoop,
    installed: list[signal.Signals],
    originals: Mapping[signal.Signals, Callable[[int, FrameType | None], object] | int | None],
) -> None:
    for sig in reversed(installed):
        try:
            loop.remove_signal_handler(sig)
            signal.signal(sig, originals[sig])
        except (OSError, RuntimeError, ValueError):
            logging.getLogger(__name__).warning("Failed to restore process signal handler")


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
    try:
        config = parse_config()
        asyncio.run(main(config))
    except McpError as error:
        prefix = f"{error.error_type}: "
        sys.stderr.write(f"{prefix}{str(error).removeprefix(prefix)}\n")
        raise SystemExit(2 if isinstance(error, (ConfigurationError, PathDeniedError)) else 1) from None


if __name__ == "__main__":
    cli()
