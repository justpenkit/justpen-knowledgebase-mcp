"""Streaming decoder and chunk ownership with native tokenizer isolated."""

import codecs
import io
from unittest.mock import Mock

import pytest

from justpen_knowledgebase_mcp import query, text
from justpen_knowledgebase_mcp.errors import IndexingError, InvalidParamsError


@pytest.mark.parametrize(
    ("encoding", "bom"),
    [
        ("utf-8", codecs.BOM_UTF8),
        ("utf-16le", codecs.BOM_UTF16_LE),
        ("utf-16be", codecs.BOM_UTF16_BE),
        ("latin-1", b""),
    ],
)
def test_stream_decoder_preserves_source_bytes_and_lines(monkeypatch, encoding, bom):
    tokenizer = Mock(return_value=[])
    connection = Mock()
    connection.fts5_tokenizer.return_value = tokenizer
    monkeypatch.setattr(text.apsw, "Connection", Mock(return_value=connection))
    value = "é\r\nhello\n"
    raw = bom + value.encode(encoding)
    chunks = list(text.iter_chunks(io.BytesIO(raw), encoding=encoding, read_size=1))
    assert len(chunks) == 1
    assert chunks[0].text == value
    assert chunks[0].source_range(3, 8) == (
        len(bom) + len("é\r\n".encode(encoding)),
        len(bom) + len("é\r\nhello".encode(encoding)),
        2,
        2,
    )
    assert chunks[0].byte_end == len(raw)
    connection.close.assert_called_once()


@pytest.mark.parametrize(
    ("raw", "encoding"), [(codecs.BOM_UTF32_LE, "auto"), (codecs.BOM_UTF8 + b"a", "utf-16le"), (b"\xff", "utf-8")]
)
def test_decoder_errors_do_not_return_partial_success(monkeypatch, raw, encoding):
    connection = Mock()
    connection.fts5_tokenizer.return_value = Mock(return_value=[])
    monkeypatch.setattr(text.apsw, "Connection", Mock(return_value=connection))
    with pytest.raises(IndexingError):
        list(text.iter_chunks(io.BytesIO(raw), encoding=encoding))
    if raw == b"\xff":
        connection.close.assert_called_once()


def test_tokenizer_adapter_closes_native_owner_on_failure(monkeypatch):
    connection = Mock()
    tokenizer = Mock(return_value=[(0, 3, "cat")])
    connection.fts5_tokenizer.return_value = tokenizer
    monkeypatch.setattr(text.apsw, "Connection", Mock(return_value=connection))
    assert text.tokenize("cat") == [(0, 3, "cat")]
    assert tokenizer.call_args.args[0] == b"cat"
    assert tokenizer.call_args.kwargs == {"include_colocated": False}
    tokenizer.side_effect = RuntimeError("native failed")
    with pytest.raises(RuntimeError):
        text.tokenize("cat")
    assert connection.close.call_count == 2


def test_buffer_waits_for_complete_token_then_retains_overlap(monkeypatch):
    monkeypatch.setattr(text, "CHUNK_BYTES", 8)
    monkeypatch.setattr(text, "OVERLAP_BYTES", 3)
    state = text._ChunkBuffer("utf-8", 0, Mock())
    state.data = b"alpha beta"
    assert state.boundary([(0, 5, "alpha"), (6, 10, "beta")]) is None
    state.data += b" gamma  "
    offsets = [(0, 5, "alpha"), (6, 10, "beta"), (11, 16, "gamma")]
    monkeypatch.setattr(text, "tokens", Mock(side_effect=[offsets, [(0, 4, "beta"), (5, 10, "gamma")]]))
    chunks = list(state.emit(eof=False, check=Mock()))
    assert chunks[0].text == "alpha beta"
    assert state.data == b"gamma  "
    assert state.owner_start == 16
    assert state.raw_start == 11


def test_giant_token_gap_and_continuation_preserve_new_owner(monkeypatch):
    monkeypatch.setattr(text, "CHUNK_BYTES", 4)
    monkeypatch.setattr(text, "TOKEN_BYTES", 8)
    state = text._ChunkBuffer("utf-8", 0, Mock())
    state.data = b"a " + b"x" * 10
    monkeypatch.setattr(
        text,
        "tokens",
        Mock(side_effect=[[(0, 1, "a"), (2, 12, "x")], [(0, 4, "axxx"), (5, 9, "tail")], [(1, 5, "tail")]]),
    )
    chunks = list(state.emit(eof=False, check=Mock()))
    assert [chunk.text for chunk in chunks] == ["a ", ""]
    assert chunks[1].gap
    assert state.skipping
    state.data = b"xxx tail"
    chunks = list(state.emit(eof=True, check=Mock()))
    assert chunks[0].text == " tail"
    assert chunks[0].owner_start == 15
    assert not state.skipping


def test_query_compiler_quotes_tokens_and_bounds_before_native_use(monkeypatch):
    native = Mock(return_value=[(0, 3, 'a"b'), (4, 7, 'a"b')])
    monkeypatch.setattr(query, "tokenize", native)
    words = query.compile_text_query('a"b a"b', "words")
    assert words.tokens == ('a"b',)
    assert words.expressions == ('"a""b"',)
    literal = query.compile_text_query('a"b a"b')
    assert literal.expressions == ('"a""b a""b"',)
    for value, mode in [("x", "invalid"), ("é" * 1025, "literal")]:
        with pytest.raises(InvalidParamsError):
            query.compile_text_query(value, mode)
    native.return_value = []
    with pytest.raises(InvalidParamsError):
        query.compile_text_query("!!!")
    native.return_value = [(0, 1, str(index)) for index in range(33)]
    with pytest.raises(InvalidParamsError):
        query.compile_text_query("many")


@pytest.mark.parametrize("operator", ["eq", "ne", "in", "exists", "gt", "gte", "lt", "lte"])
def test_nested_property_compiler_binds_literals_and_tracks_ancestors(operator):
    value = ["quoted' --"] if operator == "in" else True if operator == "exists" else "quoted' --"
    expression = {
        "all": [
            {"path": "/parent/name", "op": operator, "value": value},
            {"any": [{"path": "/flag", "op": "exists", "value": False}]},
        ]
    }
    sql, bindings = query.compile_filter(expression, "nodes")
    assert sql.count("?") == len(bindings)
    assert "/parent/name" not in sql
    assert "/parent" not in sql
    assert "quoted' --" not in sql
    assert "/parent/name" in bindings
    assert "/parent" in bindings
    if operator != "exists":
        assert "quoted' --" in bindings
