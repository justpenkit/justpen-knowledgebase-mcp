"""Canonical format descriptions remain distinct for discovery consumers."""

from justpen_knowledgebase_mcp.catalog import catalog_manifest


def test_alpn_description_does_not_include_sni_rules():
    formats = catalog_manifest()["formats"]
    assert "SNI" not in formats["alpn_list"]
    assert "ALPN" in formats["alpn_list"]
    assert "IP literals rejected" in formats["dns_or_explicit_empty"]
