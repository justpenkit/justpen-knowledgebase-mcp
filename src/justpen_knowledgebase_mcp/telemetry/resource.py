"""Create one explicit process resource without ambient OTel resource detection."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from opentelemetry.sdk.resources import Resource

if TYPE_CHECKING:
    from .config import TelemetryConfig


def build_resource(config: TelemetryConfig, *, service_version: str) -> Resource:
    """Add the session from its sole source after removing reserved extra fields."""
    if config.export_enabled and config.session_id is None:
        raise ValueError("Enabled telemetry requires a valid JUSTPEN_SESSION_ID")
    attributes = dict(config.resource_attributes)
    attributes.pop("justpen.session.id", None)
    attributes.update({"service.name": config.service_name, "service.version": service_version})
    attributes.setdefault("service.instance.id", str(uuid.uuid4()))
    if config.session_id is not None:
        attributes["justpen.session.id"] = config.session_id
    return Resource(attributes)
