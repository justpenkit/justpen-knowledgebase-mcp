"""Register the eleven bounded public knowledgebase tools."""

from fastmcp import FastMCP

from . import evidence, graph, maintenance, search
from .request_presence import RequestPresence


def register_all(mcp: FastMCP) -> None:
    """Install unconditional request presence and the four public tool families."""
    mcp.add_middleware(RequestPresence())
    for family in (graph, evidence, search, maintenance):
        family.register(mcp)
