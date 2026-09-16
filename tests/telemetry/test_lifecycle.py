"""CLI telemetry cleanup stays outside native owner cleanup."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from justpen_knowledgebase_mcp import __main__ as entry, app as app_module
from justpen_knowledgebase_mcp.app import create_app
from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.telemetry.events import TelemetryEvents
from justpen_knowledgebase_mcp.telemetry.middleware import TelemetryMiddleware
from justpen_knowledgebase_mcp.telemetry.runtime import TelemetryRuntime
from justpen_knowledgebase_mcp.tools.request_presence import RequestPresence


def test_app_installs_telemetry_and_keeps_unconditional_presence():
    config = ServerConfig(workspace_dir=Path("/workspace"))
    runtime = TelemetryRuntime(enabled=True, events=TelemetryEvents(logger_provider=None, meter_provider=None))
    app = create_app(config, telemetry=runtime)
    assert any(isinstance(item, RequestPresence) for item in app.middleware)
    assert any(isinstance(item, TelemetryMiddleware) for item in app.middleware)
    assert any(isinstance(item, RequestPresence) for item in create_app(config).middleware)


@pytest.mark.parametrize("failure", [False, True])
async def test_cli_closes_telemetry_after_native_teardown(monkeypatch, failure):
    timeline = []
    handle = SimpleNamespace(
        enabled=False,
        events=MagicMock(),
        asgi_middleware=list,
        shutdown=AsyncMock(side_effect=lambda: timeline.append("exporters")),
    )
    monkeypatch.setattr(entry, "_initialize_telemetry", lambda *args, **kwargs: handle)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *args: None)

    async def serve(**kwargs):
        try:
            if failure:
                raise RuntimeError("native failure")
        finally:
            timeline.append("native")

    monkeypatch.setattr(entry, "create_app", lambda *args, **kwargs: SimpleNamespace(run_async=serve))
    if failure:
        with pytest.raises(RuntimeError, match="native failure"):
            await entry.main(ServerConfig(workspace_dir=Path("/workspace")))
    else:
        await entry.main(ServerConfig(workspace_dir=Path("/workspace")))
    assert timeline == ["native", "exporters"]


async def test_app_factory_failure_still_closes_exporters(monkeypatch):
    handle = SimpleNamespace(events=MagicMock(), shutdown=AsyncMock())
    monkeypatch.setattr(entry, "_initialize_telemetry", lambda *args, **kwargs: handle)
    monkeypatch.setattr(entry, "create_app", MagicMock(side_effect=ValueError("factory failed")))
    with pytest.raises(ValueError, match="factory failed"):
        await entry.main(ServerConfig(workspace_dir=Path("/workspace")))
    handle.shutdown.assert_awaited_once()


async def test_partial_signal_registration_restores_handler_and_closes_exporters(monkeypatch):
    installed, restored = [], []
    loop = asyncio.get_running_loop()

    def install(sig, callback):
        if installed:
            raise RuntimeError("signal registration failed")
        installed.append(sig)

    handle = SimpleNamespace(events=MagicMock(), shutdown=AsyncMock())
    monkeypatch.setattr(entry, "_initialize_telemetry", lambda *args, **kwargs: handle)
    monkeypatch.setattr(entry, "create_app", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(loop, "add_signal_handler", install)
    monkeypatch.setattr(loop, "remove_signal_handler", restored.append)
    monkeypatch.setattr(entry.signal, "signal", lambda *args: None)
    with pytest.raises(RuntimeError, match="signal registration failed"):
        await entry.main(ServerConfig(workspace_dir=Path("/workspace")))
    assert restored == installed
    handle.shutdown.assert_awaited_once()


@pytest.mark.parametrize("enabled", [None, False, True])
async def test_factory_lifespan_injects_events_before_ready_and_stops_before_owner_close(monkeypatch, enabled):

    timeline, parameters, captured = [], {}, {}
    service = object()

    @asynccontextmanager
    async def opened(config, **kwargs):
        parameters.update(kwargs)
        timeline.append("owner-open")
        try:
            yield service
        finally:
            timeline.append("owner-close")

    events = MagicMock()
    events.lifecycle.side_effect = lambda name, attrs: timeline.append(name)
    runtime = None if enabled is None else TelemetryRuntime(enabled=enabled, events=events)

    def server_factory(*args, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(app_module.KnowledgeBase, "open", opened)
    monkeypatch.setattr(app_module, "KnowledgeBaseMCP", server_factory)
    monkeypatch.setattr(app_module, "register_all", lambda server: None)
    server = app_module.create_app(ServerConfig(workspace_dir=Path("/workspace")), telemetry=runtime)

    async def body():
        async with captured["lifespan"](server) as context:
            assert context["knowledgebase"] is service
            timeline.append("body")
            raise ValueError("body")

    with pytest.raises(ValueError, match="body"):
        await body()
    assert parameters["_telemetry_events"] is (events if enabled else None)
    assert timeline == (
        ["owner-open", "body", "owner-close"]
        if enabled is None
        else ["owner-open", "mcp.server.ready", "body", "mcp.server.stopping", "owner-close"]
    )
