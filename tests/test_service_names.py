import json
from importlib import import_module
from types import ModuleType

import pytest


def _module() -> ModuleType:
    return import_module("justpen_knowledgebase_mcp.service_names")


def test_registry_metadata_and_lookups():
    service_names = _module()
    service_names._load_registry.cache_clear()

    assert service_names.REGISTRY_VERSION == 2
    assert service_names.is_service_name("http") is True
    assert service_names.secure_required("http") is True
    assert service_names.is_service_name("ssh") is True
    assert service_names.secure_required("ssh") is False
    assert service_names.is_service_name("unknown") is True
    assert service_names.is_service_name("definitely-not-registered") is False
    assert service_names.secure_required("definitely-not-registered") is False


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"schema_version": 2, "charset": "(", "whitelist": {}},
        {
            "schema_version": 2,
            "charset": "^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$",
            "whitelist": [],
        },
        {
            "schema_version": 2,
            "charset": "^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$",
            "whitelist": {"http": {"secure_required": "yes"}},
        },
        {
            "schema_version": 2,
            "charset": ".*",
            "whitelist": {"Bad Name": {"secure_required": False}},
        },
    ],
)
def test_malformed_registry_is_rejected(monkeypatch, document):
    service_names = _module()
    service_names._load_registry.cache_clear()
    encoded = json.dumps(document).encode()
    monkeypatch.setattr(service_names, "_read_resource_bytes", lambda _name: encoded)

    with pytest.raises((RuntimeError, TypeError), match="service name registry"):
        service_names.is_service_name("http")


def test_registry_is_cached_after_first_use(monkeypatch):
    service_names = _module()
    service_names._load_registry.cache_clear()
    read_resource = service_names._read_resource_bytes
    reads: list[str] = []

    def count_resource_read(name: str) -> bytes:
        reads.append(name)
        return read_resource(name)

    monkeypatch.setattr(service_names, "_read_resource_bytes", count_resource_read)

    assert service_names.is_service_name("http") is True
    assert service_names.secure_required("ssh") is False
    assert reads == ["service_names.json"]
