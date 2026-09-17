"""Side-effect-free FastMCP application factory."""

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractContextManager, asynccontextmanager
from importlib.metadata import version

from fastmcp import FastMCP

from .config import ServerConfig
from .service import KnowledgeBase
from .shutdown import ShutdownObserver
from .stdio import KnowledgeBaseMCP
from .telemetry.middleware import TelemetryMiddleware
from .telemetry.runtime import TelemetryRuntime
from .tools import register_all
from .tools.request_presence import get_service
from .workspace import WorkspacePaths

__all__ = ["create_app", "get_service"]


def create_app(
    config: ServerConfig,
    *,
    runtime_context: Callable[[WorkspacePaths], AbstractContextManager[None]] | None = None,
    _shutdown_observer: ShutdownObserver | None = None,
    telemetry: TelemetryRuntime | None = None,
) -> FastMCP:
    """Bind one immutable configuration; resources open only during lifespan."""

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncGenerator[dict[str, KnowledgeBase]]:
        async with KnowledgeBase.open(
            config,
            runtime_context=runtime_context,
            _shutdown_observer=_shutdown_observer,
            _telemetry_events=telemetry.events if telemetry is not None and telemetry.enabled else None,
        ) as service:
            if telemetry is not None:
                telemetry.events.lifecycle("mcp.server.ready", {"justpen.transport": config.transport})
            try:
                yield {"knowledgebase": service}
            finally:
                if telemetry is not None:
                    telemetry.events.lifecycle("mcp.server.stopping", {})

    server = KnowledgeBaseMCP(
        "justpen-knowledgebase-mcp",
        version=version("justpen-knowledgebase-mcp"),
        lifespan=lifespan,
        strict_input_validation=True,
    )
    if telemetry is not None and telemetry.enabled:
        server.add_middleware(TelemetryMiddleware(events=telemetry.events, transport=config.transport))
    register_all(server)
    return server
