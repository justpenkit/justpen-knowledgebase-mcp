"""Strict streaming text projections with SQLite's actual unicode61 boundaries."""

from __future__ import annotations

import codecs
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, BinaryIO, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

import apsw

from .errors import IndexingError

CHUNK_BYTES = 65536
OVERLAP_BYTES = 2048
TOKEN_BYTES = 1024 * 1024


def tokenize(value: str) -> list[tuple[int, int, str]]:
    """Materialize unicode61 UTF-8 offsets with an explicitly scoped tokenizer owner."""
    connection = apsw.Connection(":memory:")
    try:
        return tokens(connection.fts5_tokenizer("unicode61"), value.encode("utf-8"))
    finally:
        connection.close()


def tokens(tokenizer: apsw.FTS5Tokenizer, value: bytes) -> list[tuple[int, int, str]]:
    """Call the locked native tokenizer; offsets refer to input UTF-8 bytes."""
    return cast(
        "list[tuple[int, int, str]]", tokenizer(value, apsw.FTS5_TOKENIZE_DOCUMENT, None, include_colocated=False)
    )


def record_text_units(record: dict[str, Any]) -> Iterator[tuple[str, str]]:
    """Visit every canonical string leaf, preserving its record-relative pointer."""
    metadata = record.get("metadata", {})
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    if isinstance(metadata.get("label"), str):
        yield "/label", metadata["label"]
    properties = record.get("properties", {})
    if isinstance(properties, str):
        properties = json.loads(properties)

    def visit(value: object, pointer: str) -> Iterator[tuple[str, str]]:
        if isinstance(value, str):
            yield pointer, value
        elif isinstance(value, dict):
            for key, child in cast("dict[str, Any]", value).items():
                yield from visit(child, pointer + "/" + key.replace("~", "~0").replace("/", "~1"))
        elif isinstance(value, list):
            for index, child in enumerate(cast("list[Any]", value)):
                yield from visit(child, pointer + "/" + str(index))

    yield from visit(properties, "/properties")


def detect_encoding(prefix: bytes, encoding: str) -> tuple[str, int]:
    """Detect UTF-32 before UTF-16 and reject conflicting explicit declarations."""
    if prefix.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        raise IndexingError("UNSUPPORTED_ENCODING")
    for bom, found in (
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16le"),
        (codecs.BOM_UTF16_BE, "utf-16be"),
    ):
        if prefix.startswith(bom):
            if encoding not in ("auto", found):
                raise IndexingError("ENCODING_BOM_CONFLICT")
            return found, len(bom)
    return ("utf-8" if encoding == "auto" else encoding), 0


def line_advance(value: str, line: int, *, previous_cr: bool = False) -> tuple[int, bool]:
    """Count CRLF once, and lone CR/LF as source line breaks."""
    for char in value:
        if char == "\r" or (char == "\n" and not previous_cr):
            line += 1
        previous_cr = char == "\r"
    return line, previous_cr


@dataclass(frozen=True)
class TextChunk:
    """One bounded projection; raw offsets include a removed leading BOM."""

    text: str
    encoding: str
    byte_start: int
    line_start: int
    owner_start: int
    previous_cr: bool = False
    gap: bool = False

    @property
    def byte_end(self) -> int:
        """Return the exclusive raw-source end."""
        return self.byte_start + len(self.text.encode(self.encoding))

    def source_range(self, start: int, end: int) -> tuple[int, int, int, int]:
        """Map Python character bounds to exact raw bytes and one-based lines."""
        before, match = self.text[:start], self.text[start:end]
        first, cr = line_advance(before, self.line_start, previous_cr=self.previous_cr)
        last, _ = line_advance(match[:-1], first, previous_cr=cr)
        byte_start = self.byte_start + len(before.encode(self.encoding))
        return byte_start, byte_start + len(match.encode(self.encoding)), first, last


class _ChunkBuffer:
    """Bounded decoder/token carry state owned entirely by one I/O lane."""

    def __init__(self, encoding: str, bom_size: int, tokenizer: apsw.FTS5Tokenizer) -> None:
        self.encoding, self.tokenizer = encoding, tokenizer
        self.data = b""
        self.raw_start, self.line, self.previous_cr = bom_size, 1, False
        self.owner_start = bom_size
        self.skipping = False
        self.scan_at = CHUNK_BYTES

    def chunk(self, data: bytes, *, gap: bool = False) -> TextChunk:
        return TextChunk(
            data.decode("utf-8"), self.encoding, self.raw_start, self.line, self.owner_start, self.previous_cr, gap
        )

    def consume(self, count: int) -> None:
        removed = self.data[:count].decode("utf-8")
        self.raw_start += len(removed.encode(self.encoding))
        self.line, self.previous_cr = line_advance(removed, self.line, previous_cr=self.previous_cr)
        self.data = self.data[count:]

    def skip_continuation(self) -> None:
        if self.skipping and self.data:
            initial = tokens(self.tokenizer, b"a" + self.data)[0][1] - 1
            self.consume(initial)
            if self.data:
                self.skipping = False
                self.owner_start = self.raw_start
                self.scan_at = CHUNK_BYTES

    def skip_giant(self, start: int, end: int, *, eof: bool) -> Iterator[TextChunk]:
        if start:
            yield self.chunk(self.data[:start])
            self.consume(start)
            end -= start
        yield self.chunk(b"", gap=True)
        self.skipping = end == len(self.data) and not eof
        self.consume(end)
        self.owner_start = self.raw_start
        self.scan_at = CHUNK_BYTES

    def boundary(self, offsets: list[tuple[int, int, str]]) -> int | None:
        cut = CHUNK_BYTES
        for start, end, _ in offsets:
            if start < cut < end:
                cut = end
                break
        if offsets and cut >= offsets[-1][0] and offsets[-1][1] == len(self.data):
            self.scan_at = len(self.data) + CHUNK_BYTES
            return None
        if cut > len(self.data):
            return None
        while cut < len(self.data) and self.data[cut] & 0xC0 == 0x80:
            cut += 1
        return cut

    def retain_overlap(self, cut: int, offsets: list[tuple[int, int, str]]) -> None:
        next_owner = self.raw_start + len(self.data[:cut].decode("utf-8").encode(self.encoding))
        overlap = max(0, cut - OVERLAP_BYTES)
        for start, end, _ in offsets:
            if start < overlap < end:
                overlap = start
                break
        while overlap > 0 and self.data[overlap] & 0xC0 == 0x80:
            overlap -= 1
        # An over-target token can occupy the entire chunk. No <=2048-byte
        # literal can span it; discard it rather than retaining without progress.
        self.consume(cut if overlap == 0 else overlap)
        self.owner_start = next_owner
        self.scan_at = CHUNK_BYTES + OVERLAP_BYTES

    def emit(self, *, eof: bool, check: Callable[[], None]) -> Iterator[TextChunk]:
        self.skip_continuation()
        while self.data and (eof or len(self.data) >= self.scan_at):
            check()
            offsets = tokens(self.tokenizer, self.data)
            giant = next(((a, b) for a, b, _ in offsets if b - a > TOKEN_BYTES), None)
            if giant is not None:
                yield from self.skip_giant(*giant, eof=eof)
                if self.skipping:
                    break
                continue
            if eof:
                yield self.chunk(self.data)
                self.consume(len(self.data))
                break
            cut = self.boundary(offsets)
            if cut is None:
                break
            yield self.chunk(self.data[:cut])
            self.retain_overlap(cut, offsets)


def iter_chunks(
    stream: BinaryIO, *, encoding: str = "auto", read_size: int = CHUNK_BYTES, check: Callable[[], None] = lambda: None
) -> Iterator[TextChunk]:
    """Decode strictly with bounded native token carry and explicit skipped-token gaps."""
    prefix = stream.read(4)
    effective, bom_size = detect_encoding(prefix, encoding)
    decoder = codecs.getincrementaldecoder(effective)(errors="strict")
    connection = apsw.Connection(":memory:")
    state = _ChunkBuffer(effective, bom_size, connection.fts5_tokenizer("unicode61"))
    try:
        incoming = prefix[bom_size:]
        while True:
            check()
            eof = not incoming
            try:
                state.data += decoder.decode(incoming, final=eof).encode("utf-8")
            except UnicodeError as exc:
                raise IndexingError("TEXT_DECODE_FAILED") from exc
            yield from state.emit(eof=eof, check=check)
            if eof:
                break
            incoming = stream.read(read_size)
    finally:
        connection.close()
