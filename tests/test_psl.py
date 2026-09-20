from importlib import import_module
from types import ModuleType

import pytest

from justpen_knowledgebase_mcp.errors import ExpectedValidationError


def _module() -> ModuleType:
    return import_module("justpen_knowledgebase_mcp.psl")


def test_classifies_icann_and_fallback_names():
    psl = _module()
    psl._load_rules.cache_clear()

    assert psl.classify_dns_name("corp.internal") == "domain"
    assert psl.classify_dns_name("a.corp.internal") == "subdomain"
    assert psl.classify_dns_name("api.dev.example.com") == "subdomain"


def test_rejects_public_suffix_and_single_label_names():
    psl = _module()
    psl._load_rules.cache_clear()

    with pytest.raises(ExpectedValidationError, match="public suffix"):
        psl.classify_dns_name("com")
    with pytest.raises(ExpectedValidationError, match="at least two labels"):
        psl.classify_dns_name("localhost")


def test_applies_wildcard_and_exception_rules():
    psl = _module()
    psl._load_rules.cache_clear()

    with pytest.raises(ExpectedValidationError, match="public suffix"):
        psl.classify_dns_name("foo.ck")
    assert psl.classify_dns_name("a.foo.ck") == "domain"
    assert psl.classify_dns_name("www.ck") == "domain"
    assert psl.classify_dns_name("a.www.ck") == "subdomain"


def test_checksum_mismatch_fails_closed(monkeypatch):
    psl = _module()
    psl._load_rules.cache_clear()
    read_resource = psl._read_resource_bytes

    def read_corrupted_resource(name: str) -> bytes:
        data = read_resource(name)
        if name == "public_suffix_list_icann.txt":
            return data + b"\n"
        return data

    monkeypatch.setattr(psl, "_read_resource_bytes", read_corrupted_resource)

    with pytest.raises(RuntimeError, match="PSL snapshot checksum mismatch"):
        psl.classify_dns_name("example.com")


def test_rules_are_cached_after_first_use(monkeypatch):
    psl = _module()
    psl._load_rules.cache_clear()
    read_resource = psl._read_resource_bytes
    reads: list[str] = []

    def count_resource_read(name: str) -> bytes:
        reads.append(name)
        return read_resource(name)

    monkeypatch.setattr(psl, "_read_resource_bytes", count_resource_read)

    assert psl.classify_dns_name("example.com") == "domain"
    assert psl.classify_dns_name("api.example.com") == "subdomain"
    assert sorted(reads) == ["public_suffix_list_icann.json", "public_suffix_list_icann.txt"]
