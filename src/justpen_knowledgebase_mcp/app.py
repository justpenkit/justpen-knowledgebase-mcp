"""Side-effect-free FastMCP application factory."""

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractContextManager, asynccontextmanager

from fastmcp import Context, FastMCP

from .config import ServerConfig
from .service import KnowledgeBase
from .tools import register_all
from .workspace import WorkspacePaths


def create_app(
    config: ServerConfig, *, runtime_context: Callable[[WorkspacePaths], AbstractContextManager[None]] | None = None
) -> FastMCP:
    """Bind one immutable configuration; resources open only during lifespan."""

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncGenerator[dict[str, KnowledgeBase]]:
        async with KnowledgeBase.open(config, runtime_context=runtime_context) as service:
            yield {"knowledgebase": service}

    server = FastMCP("justpen-knowledgebase-mcp", lifespan=lifespan)
    register_all(server)
    return server


def get_service(context: Context) -> KnowledgeBase:
    """Resolve the owning service for a public tool wrapper."""
    service = context.lifespan_context.get("knowledgebase")
    if not isinstance(service, KnowledgeBase):
        raise TypeError("knowledgebase lifespan is not active")
    return service
