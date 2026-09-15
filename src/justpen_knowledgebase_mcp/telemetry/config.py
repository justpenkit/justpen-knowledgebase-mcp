"""Parse knowledge-base-scoped settings without inheriting the harness's OTel configuration."""

from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal
from urllib.parse import unquote

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)
PREFIX = "JUSTPEN_KNOWLEDGEBASE_OTEL_"
_SIGNALS = ("TRACES", "LOGS", "METRICS")
_EXPORT_OPTIONS = (
    "ENDPOINT",
    "HEADERS",
    "TIMEOUT",
    "CERTIFICATE",
    "CLIENT_KEY",
    "CLIENT_CERTIFICATE",
    "COMPRESSION",
    "INSECURE",
)
_BATCH_OPTIONS = ("MAX_QUEUE_SIZE", "MAX_EXPORT_BATCH_SIZE", "SCHEDULE_DELAY", "EXPORT_TIMEOUT")
_SAMPLERS = frozenset(
    {
        "always_on",
        "always_off",
        "traceidratio",
        "parentbased_always_on",
        "parentbased_always_off",
        "parentbased_traceidratio",
    }
)
_IDENTITY = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")


@dataclass(frozen=True)
class SignalConfig:
    """An enabled OTLP signal and its transport encoding."""

    exporter: Literal["none", "otlp"]
    protocol: Literal["http/protobuf", "grpc"]


@dataclass(frozen=True)
class TelemetryConfig:
    """Immutable startup snapshot; credentials are never included in its repr."""

    export_enabled: bool
    traces: SignalConfig
    logs: SignalConfig
    metrics: SignalConfig
    session_id: str | None
    service_name: str
    resource_attributes: Mapping[str, str] = field(repr=False)
    sdk_environment: Mapping[str, str] = field(repr=False)
    shutdown_timeout_ms: int

    @property
    def enabled(self) -> bool:
        """Whether the process needs providers and exporters."""
        return self.export_enabled and any(
            signal.exporter == "otlp" for signal in (self.traces, self.logs, self.metrics)
        )


def _invalid(name: str) -> None:
    logger.warning("Ignoring invalid telemetry setting %s", name)


def _boolean(env: Mapping[str, str], name: str, *, default: bool) -> bool:
    value = env.get(name)
    if value is None or value == "":
        return default
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    _invalid(name)
    return default


def _positive(value: str, name: str, *, integer: bool = True) -> str | None:
    try:
        parsed = int(value) if integer else float(value)
        valid = math.isfinite(parsed) and parsed > 0
    except (ValueError, OverflowError):
        _invalid(name)
        return None
    if not valid:
        _invalid(name)
        return None
    return str(parsed)


def _identity(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if _IDENTITY.fullmatch(value):
        return value
    _invalid(name)
    return None


def _attributes(env: Mapping[str, str]) -> dict[str, str]:
    attributes: dict[str, str] = {}
    raw = env.get(PREFIX + "RESOURCE_ATTRIBUTES", "")
    for item in raw.split(","):
        if not item.strip():
            continue
        key, separator, value = item.partition("=")
        key = unquote(key.strip())
        if not separator or not key:
            _invalid(PREFIX + "RESOURCE_ATTRIBUTES")
            continue
        if key == "justpen.session.id":
            logger.warning("Ignoring reserved justpen.session.id in additional resource attributes")
            continue
        attributes[key] = unquote(value.strip())
    if "justpen.run.id" in attributes:
        run_id = _identity(attributes["justpen.run.id"], "justpen.run.id")
        if run_id is None:
            del attributes["justpen.run.id"]
    return attributes


def _signal(env: Mapping[str, str], name: str) -> SignalConfig:
    setting = PREFIX + (name + "_PROTOCOL" if PREFIX + name + "_PROTOCOL" in env else "PROTOCOL")
    protocol = env.get(setting, "http/protobuf").lower()
    if protocol not in {"grpc", "http/protobuf"}:
        _invalid(setting)
        return SignalConfig("none", "http/protobuf")
    return SignalConfig(
        "otlp" if _boolean(env, PREFIX + name + "_ENABLED", default=True) else "none",
        "grpc" if protocol == "grpc" else "http/protobuf",
    )


def _export_environment(env: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for signal in ("", "TRACES_", "LOGS_", "METRICS_"):
        for option in _EXPORT_OPTIONS:
            name = PREFIX + signal + option
            value = env.get(name)
            if value is None:
                continue
            if option == "TIMEOUT":
                value = _positive(value, name, integer=False)
            if option == "INSECURE":
                value = str(_boolean(env, name, default=False)).lower()
            if option == "COMPRESSION" and value is not None:
                value = value.lower()
                if value not in {"none", "gzip", "deflate"}:
                    _invalid(name)
                    value = None
            if value is not None:
                result["OTEL_EXPORTER_OTLP_" + signal + option] = value
    return result


def _batch_environment(env: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    options = [f"{batch}_{option}" for batch in ("BSP", "BLRP") for option in _BATCH_OPTIONS]
    options.extend(("METRIC_EXPORT_INTERVAL", "METRIC_EXPORT_TIMEOUT"))
    for option in options:
        name = PREFIX + option
        if name in env:
            value = _positive(env[name], name)
            if value is not None:
                result["OTEL_" + option] = value
    for batch in ("BSP", "BLRP"):
        queue = int(result.get(f"OTEL_{batch}_MAX_QUEUE_SIZE", "2048"))
        size = int(result.get(f"OTEL_{batch}_MAX_EXPORT_BATCH_SIZE", "512"))
        if size > queue:
            result[f"OTEL_{batch}_MAX_EXPORT_BATCH_SIZE"] = str(queue)
    return result


def _sampler_environment(env: Mapping[str, str]) -> dict[str, str]:
    sampler = env.get(PREFIX + "TRACES_SAMPLER", "parentbased_always_on").lower()
    if sampler not in _SAMPLERS:
        _invalid(PREFIX + "TRACES_SAMPLER")
        sampler = "parentbased_always_on"
    result = {"OTEL_TRACES_SAMPLER": sampler}
    value = env.get(PREFIX + "TRACES_SAMPLER_ARG")
    if value is not None:
        try:
            ratio = float(value)
        except ValueError:
            ratio = math.nan
        if math.isfinite(ratio) and 0 <= ratio <= 1:
            result["OTEL_TRACES_SAMPLER_ARG"] = str(ratio)
        else:
            _invalid(PREFIX + "TRACES_SAMPLER_ARG")
    return result


def read_config(env: Mapping[str, str]) -> TelemetryConfig:
    """Read only knowledge-base-scoped settings and the authoritative session variable."""
    attributes = _attributes(env)
    signals = [_signal(env, name) for name in _SIGNALS]
    sdk_environment = {**_export_environment(env), **_batch_environment(env), **_sampler_environment(env)}
    for name, signal in zip(_SIGNALS, signals, strict=True):
        sdk_environment[f"OTEL_EXPORTER_OTLP_{name}_PROTOCOL"] = signal.protocol
        sdk_environment[f"OTEL_{name}_EXPORTER"] = signal.exporter
    enabled = _boolean(env, PREFIX + "ENABLED", default=False)
    sdk_environment["OTEL_SDK_DISABLED"] = str(not enabled).lower()
    timeout = _positive(env.get(PREFIX + "SHUTDOWN_TIMEOUT_MS", "5000"), PREFIX + "SHUTDOWN_TIMEOUT_MS")
    return TelemetryConfig(
        export_enabled=enabled,
        traces=signals[0],
        logs=signals[1],
        metrics=signals[2],
        session_id=_identity(env.get("JUSTPEN_SESSION_ID"), "JUSTPEN_SESSION_ID"),
        service_name=env.get(PREFIX + "SERVICE_NAME") or attributes.get("service.name") or "justpen-knowledgebase-mcp",
        resource_attributes=MappingProxyType(attributes),
        sdk_environment=MappingProxyType(sdk_environment),
        shutdown_timeout_ms=int(timeout or "5000"),
    )


def configure_sdk_environment(config: TelemetryConfig) -> None:
    """Isolate the CLI before importing SDKs that can resolve ambient providers."""
    for name in tuple(os.environ):
        if name.startswith("OTEL_"):
            del os.environ[name]
    os.environ.update(config.sdk_environment)
