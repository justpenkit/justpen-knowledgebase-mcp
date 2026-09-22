"""Real `dbstat` walks whose cost follows the selected objects, not the file."""

import time

import apsw
import pytest

from justpen_knowledgebase_mcp.errors import LimitError
from justpen_knowledgebase_mcp.status import DerivedStorage
from justpen_knowledgebase_mcp.storage.status import sample_derived_storage
from justpen_knowledgebase_mcp.storage.worker import OperationToken

pytestmark = pytest.mark.integration

# A whole-file bound at this many pages refused every database above it, whatever
# its derived objects held. 512-byte pages reach it in a 128 MiB file.
WHOLE_FILE_BOUND_PAGES = 262144
ROW = b"\0" * 3000


def build(path, table, minimum_pages):
    """Grow one table until the file crosses `minimum_pages`; keep the rest tiny."""
    connection = apsw.Connection(str(path))
    connection.pragma("page_size", 512)
    connection.pragma("journal_mode", "off")
    connection.execute("CREATE TABLE search_documents(id INTEGER PRIMARY KEY, body BLOB)")
    connection.execute("CREATE TABLE nodes(id INTEGER PRIMARY KEY, properties BLOB)")
    connection.execute("BEGIN")
    connection.execute("INSERT INTO search_documents VALUES(0,?)", (b"one",))
    written = 0
    while connection.execute("PRAGMA page_count").get <= minimum_pages:
        connection.executemany(
            f"INSERT INTO {table} VALUES(?,?)",
            [(written + offset + 1, ROW) for offset in range(2000)],
        )
        written += 2000
    connection.execute("COMMIT")
    return connection


def test_large_file_with_small_derived_objects_publishes_its_byte_figures(tmp_path):
    """The canonical bulk is not a derived object, so it must not decide the walk."""
    connection = build(tmp_path / "graph.sqlite3", "nodes", WHOLE_FILE_BOUND_PAGES)
    page_size = connection.pragma("page_size")
    assert connection.execute("PRAGMA page_count").get > WHOLE_FILE_BOUND_PAGES

    started = time.monotonic()
    sample = DerivedStorage.model_validate(sample_derived_storage(connection, OperationToken(started + 1)))

    assert sample.available
    # One root page of `search_documents`; the 128 MiB of `nodes` beside it is not read.
    assert sample.text_projection_bytes == page_size
    assert sample.fts_index_bytes == 0
    assert sample.property_index_bytes == 0
    assert time.monotonic() - started < 0.1


def test_oversized_derived_object_is_refused_by_the_deadline_alone(tmp_path):
    """`LIMIT` still reaches `refresh_derived`, from the budget rather than a fixed bound."""
    connection = build(tmp_path / "graph.sqlite3", "search_documents", 60000)
    assert connection.execute("PRAGMA page_count").get < WHOLE_FILE_BOUND_PAGES

    with pytest.raises(LimitError):
        sample_derived_storage(connection, OperationToken(time.monotonic() + 0.005))

    # The same object measured whole: nothing about it is refused except the budget.
    sample = DerivedStorage.model_validate(sample_derived_storage(connection, OperationToken(time.monotonic() + 10)))
    assert sample.available
    assert sample.text_projection_bytes is not None
    assert sample.text_projection_bytes > 20_000_000
