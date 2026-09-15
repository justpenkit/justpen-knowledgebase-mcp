"""Side-effect-free FastMCP application factory."""

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractContextManager, asynccontextmanager

from fastmcp import FastMCP

from .config import ServerConfig
from .service import KnowledgeBase
from .shutdown import ShutdownObserver
from .tools import register_all
from .tools.request_presence import get_service
from .workspace import WorkspacePaths

__all__ = ["create_app", "get_service"]


def create_app(
    config: ServerConfig,
    *,
    runtime_context: Callable[[WorkspacePaths], AbstractContextManager[None]] | None = None,
    _shutdown_observer: ShutdownObserver | None = None,
) -> FastMCP:
    """Bind one immutable configuration; resources open only during lifespan."""

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncGenerator[dict[str, KnowledgeBase]]:
        async with KnowledgeBase.open(
            config, runtime_context=runtime_context, _shutdown_observer=_shutdown_observer
        ) as service:
            yield {"knowledgebase": service}

    server = FastMCP("justpen-knowledgebase-mcp", lifespan=lifespan, strict_input_validation=True)
    register_all(server)
    return server
