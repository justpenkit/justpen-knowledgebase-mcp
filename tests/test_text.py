"""Pure unicode61 query and streaming text boundary contracts."""

import importlib
import io

import pytest

from justpen_knowledgebase_mcp.errors import IndexingError, InvalidParamsError
from justpen_knowledgebase_mcp.query import compile_text_query


def text_module():
    return importlib.import_module("justpen_knowledgebase_mcp.text")


@pytest.mark.integration
def test_locked_unicode61_offsets_and_safe_queries():
    text = text_module()
    assert text.tokenize("Été admin.example.com") == [
        (0, 5, "ete"),
        (6, 11, "admin"),
        (12, 19, "example"),
        (20, 23, "com"),
    ]
    assert compile_text_query('one OR "two"', "words").tokens == ("one", "or", "two")
    assert compile_text_query("one one", "words").tokens == ("one",)
    for query in ("!!!", "x" * 2049, " ".join(str(n) for n in range(33))):
        with pytest.raises(InvalidParamsError):
            compile_text_query(query, "literal")


@pytest.mark.parametrize(
    ("encoding", "bom"),
    [("utf-8", b"\xef\xbb\xbf"), ("utf-16le", b"\xff\xfe"), ("utf-16be", b"\xfe\xff"), ("latin-1", b"")],
)
@pytest.mark.integration
def test_stream_offsets_lines_and_bom(encoding, bom):
    text = text_module()
    value = "é\r\n" + ("𐐀" if encoding != "latin-1" else "é") + " admin.example.com\n"
    raw = bom + value.encode(encoding)
    chunks = list(text.iter_chunks(io.BytesIO(raw), encoding=encoding, read_size=3))
    assert "".join(c.text for c in chunks) == value
    chunk = chunks[0]
    start = value.index("admin")
    assert chunk.source_range(start, start + 17) == (
        len(bom) + len(value[:start].encode(encoding)),
        len(bom) + len(value[: start + 17].encode(encoding)),
        2,
        2,
    )
    assert chunk.byte_end == len(raw)


@pytest.mark.parametrize(
    ("raw", "encoding"),
    [
        (b"\xff\xfe\x00\x00", "auto"),
        (b"\x00\x00\xfe\xff", "auto"),
        (b"\xef\xbb\xbfa", "utf-16le"),
        (b"\xff", "auto"),
        (b"\xff\xfe\x00\xd8", "auto"),
    ],
)
@pytest.mark.integration
def test_decode_errors_are_explicit(raw, encoding):
    with pytest.raises(IndexingError):
        list(text_module().iter_chunks(io.BytesIO(raw), encoding=encoding))


def test_full_record_leaves_are_independent_of_scalar_budget():
    text = text_module()
    record = {
        "metadata": {"label": "host", "source": "excluded"},
        "key": "excluded",
        "properties": {"a/b": ["x" * 2048, True, 12, None], **{f"p{i}": "leaf" for i in range(600)}},
    }
    units = list(text.record_text_units(record))
    assert units[0] == ("/label", "host")
    assert ("/properties/a~1b/0", "x" * 2048) in units
    assert len(units) == 602


@pytest.mark.integration
def test_chunk_overlap_and_long_token_gap():
    text = text_module()
    prefix = "é " * 22000
    value = prefix + "admin.example.com " + "z" * (1024 * 1024 + 1) + " tail token"
    chunks = list(text.iter_chunks(io.BytesIO(value.encode()), read_size=123))
    assert all(len(c.text.encode()) <= 65536 + 2048 + 1024 * 1024 + 8 for c in chunks)
    assert any(c.gap for c in chunks)
    assert not any("admin.example.com  tail" in c.text for c in chunks)
    assert any("tail token" in c.text for c in chunks)
    needle = "é é admin.example.com"
    assert any(needle in c.text for c in chunks)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16le", "utf-16be"])
@pytest.mark.integration
def test_multibyte_chunk_crossing_preserves_exact_source_ranges(encoding):
    text = text_module()
    prefix = "é𐐀 \r\n" * 8000
    raw = prefix.encode(encoding)
    chunks = list(text.iter_chunks(io.BytesIO(raw), encoding=encoding, read_size=127))
    assert len(chunks) > 1
    for chunk in chunks:
        assert raw[chunk.byte_start : chunk.byte_end].decode(encoding) == chunk.text
        if chunk.text:
            first = chunk.source_range(0, 1)
            expected_line = prefix[: len(raw[: chunk.byte_start].decode(encoding))].count("\n") + 1
            assert first[2] == expected_line
