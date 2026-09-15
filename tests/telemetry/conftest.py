import logging

import pytest

from .harness import Collector


@pytest.fixture
def collector(request):
    receiver = Collector(getattr(request, "param", "http/protobuf"))
    try:
        yield receiver
    finally:
        receiver.close()


@pytest.fixture(autouse=True)
def telemetry_loggers(monkeypatch):
    """Isolate component loggers from Commitizen's collection-time dictConfig."""
    for module in ("config", "events", "export", "runtime"):
        logger = logging.getLogger("justpen_knowledgebase_mcp.telemetry." + module)
        monkeypatch.setattr(logger, "disabled", False)
    for name in ("", "fastmcp"):
        for handler in logging.getLogger(name).handlers:
            monkeypatch.setattr(handler, "filters", list(handler.filters))
