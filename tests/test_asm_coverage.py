"""Prove that every field an ASM or OSINT source emits has a declared home, and that the homes hold.

Each fixture under `tests/fixtures/asm/` is derived from the tool's documented output schema. Its
mapping sends every emitted field to a catalog property, to evidence, or to `non_storable` with a
reason, and its writes are what an agent sends for that output. These tests hold the three to each
other in both directions; `tests/tools/test_asm_fixtures.py` replays the writes
through the MCP tools.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

import pytest

from justpen_knowledgebase_mcp.catalog import catalog_view, validate_record
from justpen_knowledgebase_mcp.identity import identity_key
from justpen_knowledgebase_mcp.models import WriteRequest

from . import asm_harness as harness

SOURCES = harness.source_names()

# The 24 sources the plan names; a missing or extra directory fails here rather than silently.
EXPECTED_SOURCES = {
    "subfinder",
    "dnsx",
    "naabu",
    "httpx",
    "tlsx",
    "katana",
    "nuclei",
    "asnmap",
    "cdncheck",
    "nmap",
    "bbot",
    "rdap_domain",
    "whois_gtld",
    "rdap_ip",
    "rdap_autnum",
    "trufflehog",
    "gitleaks",
    "leak_corpus",
    "github_repository",
    "aws_sts_identity",
    "nvd_cve",
    "first_epss",
    "mta_sts",
    "entra_realm",
}

# Properties no ASM or OSINT source in scope emits; each is fed by a reference dataset instead.
CONVENTION_ONLY = {"nodes.cwe.name": "fed by the MITRE CWE catalog, a reference dataset outside the source list"}

# Types catalog v3 added; each must be written by at least one fixture.
V3_TYPES = {
    ("nodes", "whois_registration"),
    ("nodes", "cloud_account"),
    ("nodes", "cloud_resource"),
    ("relations", "has_registration"),
    ("relations", "in_account"),
    ("relations", "hosted_on"),
    ("relations", "authenticates"),
    ("relations", "links_to"),
}

# Pinned so that widening what may leave a redacted field is a visible review item.
PINNED_REDACTED_DERIVATIONS = {
    ("nuclei", "matched-at", ("url_without_query",), "nodes.endpoint.url"),
    ("bbot", "{type=FINDING,module=nuclei}.data_json.description", ("bbot_nuclei_template",), "nodes.finding.rule"),
    ("bbot", "{type=FINDING,module=nuclei}.data_json.description", ("bbot_nuclei_matcher",), "nodes.finding.matcher"),
    ("bbot", "{type=FINDING,module=nuclei}.data_json.description", ("bbot_nuclei_template_id",), "nodes.finding.title"),
    (
        "bbot",
        "{type=FINDING,module=nuclei}.data_json.description",
        ("bbot_without_extracted_data",),
        "nodes.finding.description",
    ),
    ("nuclei", "request", ("http_request_method",), "nodes.endpoint.method"),
    ("trufflehog", "Raw", ("trufflehog_secret_part", "sha256_hex"), "nodes.secret.value_sha256"),
    ("trufflehog", "Raw", ("trufflehog_public_part",), "nodes.secret.key_id"),
    ("gitleaks", "Secret", ("sha256_hex",), "nodes.secret.value_sha256"),
    ("leak_corpus", "result[].password", ("sha256_hex",), "nodes.secret.value_sha256"),
    ("leak_corpus", "result[].hashed_password", ("sha256_hex",), "nodes.secret.value_sha256"),
    (
        "bbot",
        "{type=FINDING,module=trufflehog}.data_json.description",
        ("bbot_trufflehog_secret_part", "sha256_hex", "hex_prefix16"),
        "nodes.finding.matcher",
    ),
}

SECRET_SOURCES = ("trufflehog", "gitleaks", "leak_corpus", "bbot")


@pytest.fixture(scope="module", params=SOURCES)
def source(request: pytest.FixtureRequest) -> harness.Source:
    return harness.load_source(request.param)


def test_the_fixture_set_is_the_planned_source_list() -> None:
    assert set(SOURCES) == EXPECTED_SOURCES


def test_every_fixture_leaf_is_mapped(source: harness.Source) -> None:
    """Test 1: a field the tool emits without a mapping row fails, naming its path."""
    unmapped = {
        (file_name, ".".join(segments))
        for file_name in source.files
        for record, segments, _value in harness.record_leaves(source, file_name)
        if not harness.rows_for(source, segments, record)
    }
    assert not unmapped, sorted(unmapped)


def test_every_documented_field_is_mapped(source: harness.Source) -> None:
    """Test 2: every row matches a fixture leaf, unless it declares the field documented but absent."""
    used = {
        row
        for file_name in source.files
        for record, segments, _value in harness.record_leaves(source, file_name)
        for row in harness.rows_for(source, segments, record)
    }
    dead = [row.path for row in source.rows if row not in used and not row.documented_only]
    assert not dead, dead
    assert all(row not in used for row in source.rows if row.documented_only)


def test_every_sink_is_declared(source: harness.Source) -> None:
    """Test 3: sinks name declared properties, wildcards never feed one, and non-storables say why."""
    for row in source.rows:
        head, _dot, rest = row.sink.partition(".")
        if head in ("nodes", "relations"):
            type_name, property_name = rest.split(".", 1)
            assert property_name in harness.declared_properties(head, type_name), row.path
            assert not row.wildcard, f"{row.path}: a wildcard row may not feed a typed property"
        elif head == "meta":
            assert rest in ("observed_at", "source", "label"), row.path
        else:
            assert row.sink in ("evidence", "non_storable"), row.path
        if row.sink == "non_storable":
            assert row.reason, f"{row.path}: a non-storable row states its reason"
        for name in row.transform:
            assert name in harness.transforms.TRANSFORMS, f"{row.path}: unknown transform {name}"
        assert row.when is None or row.when in harness.transforms.CONDITIONS, row.path
    for batch in source.batches:
        for kind in ("nodes", "relations"):
            for record in batch["request"].get(kind, []):
                undeclared = set(record.get("properties", {})) - harness.declared_properties(kind, record["type"])
                assert not undeclared, f"{record['type']} writes undeclared {sorted(undeclared)}"


def test_every_mapped_value_is_written(source: harness.Source) -> None:
    """Test 4: each mapped fixture value, transformed, is carried by a write from the same file."""
    written = harness.written_values(source)
    for file_name, sinks in harness.expected_values(source).items():
        for sink, values in sinks.items():
            missing = values - written.get(file_name, {}).get(sink, set())
            assert not missing, (file_name, sink, sorted(missing))


def test_every_written_value_traces_to_the_fixture(source: harness.Source) -> None:
    """Test 5: each written value comes from a mapping row on the same file or a declared derivation."""
    expected = harness.expected_values(source)
    derived = harness.derived_values(source)
    for file_name, sinks in harness.written_values(source).items():
        assert file_name in source.files, file_name
        for sink, values in sinks.items():
            allowed = expected.get(file_name, {}).get(sink, set()) | derived.get(sink, set())
            untraced = values - allowed
            assert not untraced, (file_name, sink, sorted(untraced))
    for item in source.derived:
        assert item.get("reason"), f"{harness.sink_key(item['sink'])}: a derivation states its reason"


def test_every_write_validates(source: harness.Source) -> None:
    """Test 6: every batch fits `WriteRequest` and every record passes the catalog and hashes."""
    for index in range(len(source.batches)):
        request = harness.resolved_request(source, index)
        WriteRequest.model_validate(request)
        records = len(request.get("nodes", [])) + len(request.get("relations", []))
        assert records <= 100, f"batch {index} holds {records} records; evidence links cap it at 100"
        for kind in ("nodes", "relations"):
            for record in request.get(kind, []):
                properties = json.loads(json.dumps(record["properties"]))
                validate_record(kind, record["type"], properties)
                assert properties == record["properties"], f"{record['type']}: write a canonical spelling"
    assert harness.node_identities(source)


def test_every_new_type_and_property_is_fed() -> None:
    """Test 7: every v3 type is written somewhere and every declared attribute has a source."""
    sinks: set[str] = set()
    written_types: set[tuple[str, str]] = set()
    for name in SOURCES:
        source = harness.load_source(name)
        sinks |= {row.sink for row in source.rows}
        sinks |= set(harness.derived_values(source))
        for batch in source.batches:
            for kind in ("nodes", "relations"):
                written_types |= {(kind, record["type"]) for record in batch["request"].get(kind, [])}
    assert written_types >= V3_TYPES, sorted(V3_TYPES - written_types)
    needed = {
        f"{kind}.{type_name}.{name}"
        for kind in ("nodes", "relations")
        for type_name, definition in catalog_view()[kind].items()
        for name in (
            definition["optional"]
            if (kind, type_name) not in V3_TYPES
            else {*definition["required"], *definition["optional"]}
        )
    } | {"nodes.finding.rule", "nodes.finding.matcher"}
    unfed = needed - sinks - set(CONVENTION_ONLY)
    assert not unfed, sorted(unfed)


def test_secret_rows_redact_and_type() -> None:
    """Test 8: secret-bearing fields are redacted, only allowlisted derivations leave them, and every
    scanner or leak secret carries a typed `kind`."""
    assert harness.transforms.REDACTED_DERIVATIONS == PINNED_REDACTED_DERIVATIONS
    for name in SOURCES:
        source = harness.load_source(name)
        redacted_paths = {row.path for row in source.rows if row.redact}
        missing = harness.transforms.SECRET_FIELDS.get(name, frozenset()) - redacted_paths
        assert not missing, (name, sorted(missing))
        for row in source.rows:
            if row.redact and row.sink != "non_storable":
                triple = (name, row.path, row.transform, row.sink)
                assert triple in harness.transforms.REDACTED_DERIVATIONS, triple
        if name not in SECRET_SOURCES:
            continue
        for batch in source.batches:
            for node in batch["request"].get("nodes", []):
                if node["type"] == "secret":
                    assert node["properties"].get("kind", "other") != "other", (name, node)


def test_key_id_is_never_the_secret_digest() -> None:
    for name in SOURCES:
        for batch in harness.load_source(name).batches:
            for node in batch["request"].get("nodes", []):
                properties = node["properties"]
                if node["type"] == "secret" and "key_id" in properties:
                    digest = hashlib.sha256(properties["key_id"].encode("utf-8")).hexdigest()
                    assert digest != properties["value_sha256"], name


def test_no_two_fixture_results_share_an_identity(source: harness.Source) -> None:
    """Test 9: scoped result nodes, keyed under their parents, never collide unless declared."""
    scoped = set(harness.scope_relations())
    counts = Counter(
        identity for _batch, _index, type_name, identity in harness.node_identities(source) if type_name in scoped
    )
    intended = {item["identity"] for item in source.intended_merges}
    collisions = {identity for identity, count in counts.items() if count > 1} - intended
    assert not collisions, sorted(collisions)
    for item in source.intended_merges:
        assert item.get("reason"), item


def _registrations(name: str) -> dict[str, set[tuple[str, str]]]:
    source = harness.load_source(name)
    found: dict[str, set[tuple[str, str]]] = {}
    for batch in source.batches:
        for node in batch["request"].get("nodes", []):
            if node["type"] == "whois_registration":
                key = identity_key("nodes", "whois_registration", dict(node["properties"]), harness.PLACEHOLDER_PARENT)
                found.setdefault(batch["file"], set()).add((node["properties"]["registry"], key))
    return found


def test_rdap_and_whois_views_of_one_registration_share_identity() -> None:
    """Test 10: the RDAP and port-43 views of example.com merge into one registration."""
    rdap = _registrations("rdap_domain")["example.com.json"]
    whois = _registrations("whois_gtld")["example.com.txt"]
    assert rdap
    assert rdap == whois


def test_missing_handle_writes_no_registration() -> None:
    """Test 11: no handle, a redacted handle or a placeholder handle writes no registration,
    no registrar edge and no registration-role contact; the values stay evidence."""
    source = harness.load_source("rdap_domain")
    for file_name in ("example-nohandle.json", "example-placeholder.json"):
        batches = [batch["request"] for batch in source.batches if batch["file"] == file_name]
        assert batches, file_name
        for request in batches:
            assert all(node["type"] != "whois_registration" for node in request.get("nodes", [])), file_name
            for relation in request.get("relations", []):
                assert relation["type"] != "registered_through", file_name
                role = relation.get("properties", {}).get("role")
                assert role not in ("registrant", "admin", "tech", "billing"), file_name
            assert any(node["type"] == "domain" for node in request.get("nodes", [])), file_name


def test_transform_golden_cases() -> None:
    apply = harness.transforms.apply_chain
    assert apply("Collection #1", ["leakcheck_breach_token"], {}) == "leakcheck:collection-1"
    assert apply("expired", ["sni_matcher"], {"host": "A.Example.com."}) == "a.example.com:expired"
    assert apply("expired", ["sni_matcher"], {"host": "192.0.2.10"}) == "expired"
    assert apply("0A:BC:00", ["colon_hex_serial_to_lower_hex"], {}) == "abc00"
    assert apply("text/html; charset=utf-8", ["media_type_essence"], {}) == "text/html"
    assert apply("2021-12-10T10:15:09.143", ["rfc3339_utc"], {}) == "2021-12-10T10:15:09.143Z"
    assert apply(["client transfer prohibited", "active"], ["rdap_status_to_epp", "sorted_set"], {}) == [
        "clientTransferProhibited",
        "ok",
    ]
    assert apply("cpe:/a:openbsd:openssh:8.9p1", ["cpe22_uri_to_cpe23", "cpe_product_level"], {}) == (
        "cpe:2.3:a:openbsd:openssh:*:*:*:*:*:*:*:*"
    )
    aws: dict[str, Any] = {"DetectorName": "AWS", "Raw": "AKIAIOSFODNN7EXAMPLE", "RawV2": "AKIAIOSFODNN7EXAMPLE:s3cr3t"}
    assert apply("AKIAIOSFODNN7EXAMPLE", ["trufflehog_secret_part"], aws) == "s3cr3t"
    assert apply("AKIAIOSFODNN7EXAMPLE", ["trufflehog_public_part"], aws) == "AKIAIOSFODNN7EXAMPLE"
    with pytest.raises(KeyError):
        apply("x", ["trufflehog_public_part"], {"DetectorName": "Stripe"})
    with pytest.raises(KeyError):
        apply("maybe", ["whois_dnssec_bool"], {})


def test_a_bbot_trufflehog_finding_joins_the_secret_trufflehog_reports() -> None:
    """The BBOT matcher is a prefix of the secret's own digest, never of its public key id."""
    secrets = {
        node["properties"]["value_sha256"]
        for batch in harness.load_source("trufflehog").batches
        for node in batch["request"].get("nodes", [])
        if node["type"] == "secret"
    }
    matchers = {
        node["properties"]["matcher"]
        for batch in harness.load_source("bbot").batches
        for node in batch["request"].get("nodes", [])
        if node["type"] == "finding" and node["properties"]["rule"].startswith("trufflehog:")
    }
    assert matchers
    assert matchers <= {digest[:16] for digest in secrets}
