"""FTS orchestration verifies exact matches using scripted native token offsets."""

from unittest.mock import Mock

import pytest

from justpen_knowledgebase_mcp.errors import ConflictError, NotFoundError
from justpen_knowledgebase_mcp.query import TextQuery
from justpen_knowledgebase_mcp.storage import fulltext
from justpen_knowledgebase_mcp.text import TextChunk

from .helpers import EVIDENCE, NODE, OTHER, cursor, database, owner

QUERY = TextQuery("cat", "literal", ("cat",), ('"cat"',), 0, 3)


def test_refresh_full_string_leaves_and_coverage():
    db = database()
    fulltext.refresh_record_text(db, "nodes", owner())
    assert list(db.executemany.call_args.args[1]) == [(1, "/properties/name", "example.com")]
    db = database(cursor(rows=[("ready", 0, 2), ("index_failed", 1, 3), ("pending", 1, 4)]))
    assert fulltext.coverage(db) == {"ready": 2, "pending": 4, "failed": 3, "incomplete": 7, "not_applicable": 0}


def test_literal_uses_native_boundaries_and_exact_case(monkeypatch):
    monkeypatch.setattr(fulltext, "tokens", Mock(return_value=[(0, 7, "catalog"), (8, 11, "cat"), (12, 15, "cat")]))
    assert list(fulltext._literal_ranges(Mock(), "catalog cat CAT", QUERY)) == [(8, 11)]
    query = TextQuery("cat", "words", ("cat",), ('"cat"',), 0, 3)
    assert list(fulltext._document_ranges(Mock(), "catalog cat CAT", query, 0)) == [(8, 11), (12, 15)]


def test_match_unit_requires_all_terms_and_best_rank(monkeypatch):
    monkeypatch.setattr(fulltext, "tokens", Mock(return_value=[(0, 3, "cat")]))
    document = (1, "/properties/name", "cat", 0, 1, 0, "utf-8", False, -2.0)
    result = fulltext.match_unit(database(cursor(rows=[document, document])), Mock(), "nodes", 1, NODE, QUERY)
    assert result is not None
    assert result["score"] == -2
    assert len(result["matches"]) == 1
    assert result["snippet"] == "cat"
    query = TextQuery("cat dog", "words", ("cat", "dog"), ('"cat"', '"dog"'), 0, 7)
    assert fulltext.match_unit(database(cursor(rows=[document]), cursor()), Mock(), "nodes", 1, NODE, query) is None


def test_overlap_reference_and_bounded_refs():
    document = (1, None, "cat" * 200, 0, 1, 3, "utf-8", False, -1.0)
    assert fulltext._reference(document, "evidence", EVIDENCE, 0, 3) is None
    reference = fulltext._reference(document, "evidence", EVIDENCE, 0, 600)
    assert reference is not None
    assert reference[2]
    assert len(reference[1].encode()) == 512
    matches = fulltext._Matches()
    for index in range(34):
        ref = fulltext._reference(document, "nodes", NODE, index, index + 3)
        assert ref is not None
        matches.add(document, ref)
    result = matches.result(QUERY, -1)
    assert len(result["matches"]) == 32
    assert result["matches_truncated"]


def test_owner_uses_one_best_complete_evidence_unit(monkeypatch):
    match = Mock(side_effect=[None, {"score": -1}, {"score": -2}, None])
    monkeypatch.setattr(fulltext, "match_unit", match)
    result = fulltext.owner_match(
        database(cursor(rows=[(2, EVIDENCE), (3, EVIDENCE), (4, EVIDENCE)])),
        Mock(),
        "nodes",
        1,
        NODE,
        QUERY,
        include_evidence=True,
    )
    assert result == {"score": -2}
    assert match.call_count == 4


@pytest.mark.parametrize(
    ("row", "active", "error"),
    [
        (None, None, NotFoundError),
        ((1, 2, None, "auto", "text/plain", "delete_pending", 8), None, ConflictError),
        ((1, 2, 9, "auto", "text/plain", "ready", 8), 1, ConflictError),
    ],
)
def test_claim_item_respects_lifecycle_and_other_jobs(row, active, error):
    db = database(cursor(rows=[] if row is None else [row]), cursor(value=active))
    with pytest.raises(error):
        fulltext.claim_item(db, EVIDENCE, 1, OTHER)


def test_generation_fence_bounds_cleanup_and_publication():
    db = database(cursor(rows=[(1, 2, 9, "utf-8", "text/plain", "ready", 8)]), cursor(value=None), cursor())
    selected = fulltext.claim_item(db, EVIDENCE, 1, OTHER)
    assert selected.generation == 2
    assert selected.token == OTHER
    db = database()
    db.execute.return_value.get = 1
    db.changes.return_value = 0
    assert fulltext.clear_item_batch(db, selected)
    fulltext.append_chunk(db, selected, TextChunk("cat", "utf-8", 3, 1, 3))
    assert db.execute.call_args.args[1][1:4] == ("cat", 3, 6)
    count = db.execute.call_count
    fulltext.append_chunk(db, selected, TextChunk("", "utf-8", 3, 1, 3, gap=True))
    assert db.execute.call_count == count + 1
    fulltext.finish_item(db, selected, "ready", incomplete=False)
    assert db.execute.call_args.args[1] == ("ready", 0, 1, 2, 1, OTHER)
    db.execute.return_value.get = None
    with pytest.raises(ConflictError, match="INDEX_GENERATION_CHANGED"):
        fulltext.append_chunk(db, selected, TextChunk("cat", "utf-8", 3, 1, 3))


def test_saturated_references_stop_redundant_work_but_keep_later_rank(monkeypatch):
    text = "cat " * 1000
    documents = [(1, "/a", text, 0, 1, 0, "utf-8", False, -1.0), (2, "/b", text, 0, 1, 0, "utf-8", False, -3.0)]
    monkeypatch.setattr(
        fulltext, "_document_ranges", lambda *_args: ((index, index + 3) for index in range(0, 4000, 4))
    )
    reference = Mock(wraps=fulltext._reference)
    monkeypatch.setattr(fulltext, "_reference", reference)
    result = fulltext.match_unit(database(cursor(rows=documents)), Mock(), "nodes", 1, NODE, QUERY)
    assert result is not None
    assert result["score"] == -3.0
    assert len(result["matches"]) == 32
    assert result["matches_truncated"]
    assert result["snippet_truncated"]
    assert [value["byte_start"] for value in result["matches"]] == list(range(0, 128, 4))
    # Initial snippet covers512bytes; the first outside hit settles both flags.
    assert reference.call_count <= 130


def test_reference_byte_budget_does_not_hide_shorter_later_document(monkeypatch):
    text = "cat " * 1000
    documents = [
        (1, "/" + "p" * 60000, text, 0, 1, 0, "utf-8", False, -1.0),
        (2, "/short", text, 0, 1, 0, "utf-8", False, -2.0),
    ]
    monkeypatch.setattr(
        fulltext, "_document_ranges", lambda *_args: ((index, index + 3) for index in range(0, 4000, 4))
    )
    result = fulltext.match_unit(database(cursor(rows=documents)), Mock(), "nodes", 1, NODE, QUERY)
    assert result is not None
    assert result["matches_truncated"]
    assert result["snippet_truncated"]
    assert len(result["matches"]) == 32
    assert sum(value["pointer"] == "/short" for value in result["matches"]) == 31
    assert result["score"] == -2.0


def test_saturation_preserves_missing_word_and_overlap_rejection(monkeypatch):
    text = "cat " * 1000
    overlap = (1, None, text, 0, 1, 5000, "utf-8", False, -99.0)
    document = (2, None, text, 0, 1, 0, "utf-8", False, -2.0)
    monkeypatch.setattr(
        fulltext, "_document_ranges", lambda *_args: ((index, index + 3) for index in range(0, 4000, 4))
    )
    query = TextQuery("cat dog", "words", ("cat", "dog"), ('"cat"', '"dog"'), 0, 7)
    assert (
        fulltext.match_unit(
            database(cursor(rows=[overlap, document]), cursor()), Mock(), "evidence", 1, EVIDENCE, query
        )
        is None
    )
    result = fulltext.match_unit(database(cursor(rows=[overlap, document])), Mock(), "evidence", 1, EVIDENCE, QUERY)
    assert result is not None
    assert result["score"] == -2.0
