"""Identity and timestamp codecs preserve the selected exact representation."""

import hashlib
import re
from uuid import UUID, uuid4

import pytest

from justpen_knowledgebase_mcp.identity import (
    _CANONICAL_EVIDENCE,
    _CANONICAL_UUID,
    EVIDENCE_ID_PATTERN,
    GRAPH_ID_PATTERN,
    format_timestamp,
    identity_json,
    identity_key,
    parse_timestamp,
    validate_evidence_id,
    validate_graph_id,
    validate_record_id,
)

# The published pattern and the grammar its validator applies, for the two public identifier
# spellings. Nothing here restates a pattern as a literal: a hand-copied spelling is exactly the
# drift these tests exist to refuse.
PUBLISHED = [
    (GRAPH_ID_PATTERN, _CANONICAL_UUID, validate_graph_id, str(uuid4())),
    (EVIDENCE_ID_PATTERN, _CANONICAL_EVIDENCE, validate_evidence_id, "e_" + hashlib.sha256(b"body").hexdigest()),
]
PUBLISHED_IDS = ["graph", "evidence"]
# Constructs a Python `re` source may carry that ECMA-262, the dialect JSON Schema reads `pattern`
# in, either lacks or spells differently: Python-only named groups and comments, the Python-only
# end-of-input escapes, inline flag groups, and the `(?<` forms ECMA-262 only gained in ES2018 and
# an older validator still rejects. Lookahead is deliberately absent; both dialects read it alike.
NOT_PUBLISHABLE_VERBATIM = ["(?P", "(?#", "(?<", r"\A", r"\Z", r"\z", "(?i", "(?s", "(?m", "(?x", "(?a", "(?u"]

PARENT = "00000000-0000-4000-8000-000000000001"
OTHER_PARENT = "00000000-0000-4000-8000-000000000002"


def test_identity_ignores_order_and_nonidentity_fields():
    assert identity_key("nodes", "domain", {"value": "example.com"}) == identity_key(
        "nodes", "domain", {"note": "x", "value": "example.com"}
    )
    assert identity_key("relations", "resolves_to", {}) == ""


def test_scoped_identity_is_canonical_and_parent_sensitive():
    properties = {"transport": "tcp", "number": 443}
    assert identity_json("nodes", "port", properties, PARENT) == (
        '{"number":443,"parent":"00000000-0000-4000-8000-000000000001","transport":"tcp"}'
    )
    assert identity_key("nodes", "port", properties, PARENT) == identity_key(
        "nodes", "port", {"number": 443, "note": "ignored", "transport": "tcp"}, PARENT
    )
    assert identity_key("nodes", "port", properties, PARENT) != identity_key("nodes", "port", properties, OTHER_PARENT)


@pytest.mark.parametrize(
    ("type_name", "properties"),
    [
        ("port", {"transport": "tcp", "number": 443}),
        ("service", {"name": "unknown"}),
        ("finding", {"title": "Open management port", "severity": "high"}),
    ],
)
def test_scoped_identity_requires_parent(type_name, properties):
    with pytest.raises(ValueError, match="parent"):
        identity_key("nodes", type_name, properties)


def test_identity_strict_integer_and_types():
    with pytest.raises(ValueError):
        identity_key("nodes", "port", {"transport": "tcp", "number": 1.0}, PARENT)
    assert identity_key("nodes", "port", {"transport": "tcp", "number": 1}, PARENT)


def test_order_independent_relation_identity_hashes_declared_collection():
    first = {
        "flags": 0,
        "parameters": [
            {"name": "validationmethods", "value": "dns-01", "ignored": 1},
            {"name": "accounturi", "value": "https://ca.example/acct/1"},
        ],
    }
    second = {
        "flags": 0,
        "parameters": [
            {"value": "https://ca.example/acct/1", "name": "accounturi"},
            {"value": "dns-01", "name": "validationmethods", "ignored": 2},
        ],
    }
    assert identity_key("relations", "caa_issue", first) == identity_key("relations", "caa_issue", second)


CAA_URI = "https://ca.example/acct/1"


def _caa(name: str) -> dict[str, object]:
    return {"flags": 0, "parameters": [{"name": name, "value": CAA_URI}]}


@pytest.mark.parametrize("type_name", ["caa_issue", "caa_issuewild"])
def test_caa_parameter_name_case_is_one_identity(type_name: str) -> None:
    """RFC 8659 tags are case-insensitive, and the parameter list is identity-bearing."""
    assert identity_key("relations", type_name, _caa("accountURI")) == identity_key(
        "relations", type_name, _caa("accounturi")
    )
    assert identity_key("relations", type_name, _caa("validationmethods")) != identity_key(
        "relations", type_name, _caa("accounturi")
    )


def test_caa_parameter_value_case_is_still_two_identities() -> None:
    """Only the tag folds: a parameter value is a URI or a method name and stays case-sensitive."""
    other = {"flags": 0, "parameters": [{"name": "accounturi", "value": CAA_URI.upper()}]}

    assert identity_key("relations", "caa_issue", _caa("accounturi")) != identity_key("relations", "caa_issue", other)


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-01t00:00:00Z",
        "2026-01-01T00:00:00z",
        "2026-01-01 00:00:00Z",
        "2026-01-01T00:00:60Z",
        "2026-01-01T00:00:00-00:00",
        "2026-02-30T00:00:00Z",
        "2026-01-01T00:00:00.1234567Z",
        "0001-01-01T00:00:00+00:01",
        "9999-12-31T23:59:59-00:01",
    ],
)
def test_invalid_timestamp(value):
    with pytest.raises(ValueError):
        parse_timestamp(value)


def test_timestamp_normalizes_only_metadata_to_utc_microseconds():
    assert parse_timestamp("1970-01-01T01:00:00.123456+01:00") == 123456
    assert format_timestamp(123456) == "1970-01-01T00:00:00.123456Z"
    assert format_timestamp(parse_timestamp("0001-01-01T00:00:00Z")) == "0001-01-01T00:00:00.000000Z"


NON_CANONICAL = [
    "550E8400-E29B-41D4-A716-446655440000",
    "550e8400-E29B-41d4-a716-446655440000",
    "550e8400-e29b-41d4-a7164-46655440000",
    "-550e8400e29b41d4a716446655440000---",
]


@pytest.mark.parametrize("value", NON_CANONICAL)
def test_non_canonical_graph_id_is_refused_at_ingress(value):
    """SQLite compares TEXT byte for byte, so only the stored spelling can ever match a row."""
    assert UUID(value)
    with pytest.raises(ValueError, match="graph id"):
        validate_record_id("nodes", value)
    with pytest.raises(ValueError, match="graph id"):
        validate_graph_id(value)


def test_canonical_graph_id_survives_unchanged():
    canonical = str(UUID("550E8400-E29B-41D4-A716-446655440000"))
    assert validate_record_id("nodes", canonical) == canonical
    assert validate_graph_id(canonical) == canonical
    assert validate_record_id("evidence", "e_" + "0" * 64) == "e_" + "0" * 64


@pytest.mark.parametrize(
    "value",
    [
        "550e8400-e29b-41d4-a716-44665544000",
        "550e8400-e29b-41d4-a716-44665544000g",
        "urn:uuid:550e8400-e29b-41d4-a716-4466",
        "",
    ],
)
def test_malformed_graph_id_keeps_its_own_rejection(value):
    with pytest.raises(ValueError, match="invalid graph id"):
        validate_graph_id(value)


def test_the_grammar_accepts_exactly_what_the_uuid_roundtrip_accepts():
    """`storage/job_retention.py:68-77` spells this acceptance as `str(UUID(value)) != value`.

    The grammar is the cheaper spelling of the same rule, so it is held against the precedent over
    a generated corpus rather than trusted to stay equivalent by inspection.
    """
    canonical = str(uuid4())
    corpus = [canonical, canonical.upper(), canonical.replace("-", ""), f"{{{canonical}}}", f"urn:uuid:{canonical}"]
    corpus += [canonical[:index] + canonical[index:].replace("-", "", 1) + "-" for index in range(9)]
    corpus += [canonical[:index] + character + canonical[index + 1 :] for index in range(36) for character in "-gF0"]
    assert len(set(corpus)) > 100
    for value in corpus:
        try:
            roundtrip = str(UUID(value)) == value
        except (AttributeError, TypeError, ValueError):
            roundtrip = False
        grammar = True
        try:
            validate_graph_id(value)
        except ValueError:
            grammar = False
        assert grammar is roundtrip, value


def _corpus(canonical: str) -> list[str]:
    """Surround, truncate, extend and corrupt one canonical identifier, character by character."""
    values = [canonical, canonical.upper(), canonical.replace("-", ""), canonical + canonical, ""]
    values += [
        canonical[:index] + character + canonical[index + 1 :]
        for index in range(len(canonical))
        for character in "-gF0"
    ]
    values += [canonical[:index] for index in range(len(canonical))]
    values += [
        f"{prefix}{canonical}{suffix}" for prefix in ("", "x", "\n", " ") for suffix in ("", "x", "\n", " ", "\u00e9")
    ]
    return list(dict.fromkeys(values))


@pytest.mark.parametrize(("pattern", "grammar", "validator", "canonical"), PUBLISHED, ids=PUBLISHED_IDS)
def test_a_published_pattern_is_the_anchored_spelling_of_its_own_grammar(pattern, grammar, validator, canonical):
    """The schema is derived from the validator's compiled grammar, never transcribed beside it.

    `validate_graph_id` and `validate_evidence_id` were enforcing a spelling `list_tools()` never
    published, so a host could not refuse `550E8400-...` before sending it. Deriving the published
    `pattern` from the same compiled object is what keeps the two from parting again.
    """
    del validator, canonical
    assert pattern == f"^(?:{grammar.pattern})$"


@pytest.mark.parametrize(("pattern", "grammar", "validator", "canonical"), PUBLISHED, ids=PUBLISHED_IDS)
def test_a_published_pattern_accepts_exactly_what_its_validator_accepts(pattern, grammar, validator, canonical):
    """Hold the published constraint against the runtime one over a generated corpus.

    `re.fullmatch` is the Python spelling of what an ECMA-262 engine does with these anchors: a
    plain `re.search` would also accept a trailing newline, which ECMA-262's `$` does not.
    """
    del grammar
    corpus = _corpus(canonical)
    assert len(corpus) > 100
    for value in corpus:
        try:
            validator(value)
        except ValueError:
            runtime = False
        else:
            runtime = True
        assert (re.fullmatch(pattern, value) is not None) is runtime, value


@pytest.mark.parametrize(("pattern", "grammar", "validator", "canonical"), PUBLISHED, ids=PUBLISHED_IDS)
def test_a_published_pattern_is_anchored_rather_than_a_substring_search(pattern, grammar, validator, canonical):
    """JSON Schema's `pattern` is an unanchored search, so the anchors carry the whole meaning.

    This asserts on `re.search`, the operation a host actually performs, rather than `fullmatch`,
    which anchors by itself and would pass an unanchored pattern that promises nothing.
    """
    del validator
    surrounded = f"prefix{canonical}suffix"

    assert re.search(grammar.pattern, surrounded) is not None
    assert re.search(pattern, surrounded) is None
    assert re.search(pattern, canonical) is not None


@pytest.mark.parametrize(("pattern", "grammar", "validator", "canonical"), PUBLISHED, ids=PUBLISHED_IDS)
def test_a_published_pattern_stays_inside_the_subset_ecma_262_spells_identically(
    pattern, grammar, validator, canonical
):
    """A Python `re` source is not automatically a valid ECMA-262 `pattern`.

    These two grammars are character classes and counted repetitions, which both dialects read the
    same way; a real ECMA-262 engine was held against the validators over a generated corpus when
    the patterns were first published. This refuses a later grammar that reaches for a construct
    outside that shared subset, where publishing the source verbatim would hand clients something
    their own validator reads differently or cannot compile at all.
    """
    del validator, canonical
    assert grammar.flags == re.UNICODE
    for construct in NOT_PUBLISHABLE_VERBATIM:
        assert construct not in pattern, construct
