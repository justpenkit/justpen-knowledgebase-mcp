"""Side-effect-free FastMCP application factory."""

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractContextManager, asynccontextmanager
from importlib.metadata import version

from fastmcp import FastMCP

from .catalog import inventory_description
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

# Every client receives this at initialize, before it reads any tool. The testing rule is the
# sentence `kb_types` publishes in its `inventory` block, so the two cannot drift (KTD8).
_INSTRUCTIONS = " ".join(
    (
        "One engagement's recon graph and evidence; the server performs no scanning.",
        "Creating a node whose `kb_types` inventory is `carries` requires an `ownership`: `owned` for the",
        "target's own assets, `dependency` for third-party infrastructure the target relies on, `candidate`",
        "while attribution is unconfirmed.",
        "Authorization is a separate axis: `in_scope`, `out_of_scope` or `unknown`.",
        inventory_description()["testing"],
        "Leave unrelated neighbors, such as other tenants of a shared IP address or unrelated reverse-IP",
        "and certificate names, in evidence, and never write them as nodes.",
        "When the contract allows only listed assets under an `in_scope` root, such as ports 80 and 443 on",
        "one host, mark that root `allowlist_scoped` with evidence: its scoped children are then",
        "`out_of_scope` until an ID write with evidence widens them to `in_scope`.",
        "Before writing a scanner batch, drop the names a `kb_search` with `ownership=rejected` returns;",
        "one rejected identity refuses the whole batch.",
        "Call `kb_types` before writing.",
    )
)


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
        instructions=_INSTRUCTIONS,
        lifespan=lifespan,
        strict_input_validation=True,
    )
    if telemetry is not None and telemetry.enabled:
        server.add_middleware(TelemetryMiddleware(events=telemetry.events, transport=config.transport))
    register_all(server)
    return server
