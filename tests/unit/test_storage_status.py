"""Storage sample failure and streaming cancellation without a native database."""

from unittest.mock import Mock

import apsw
import pytest

from justpen_knowledgebase_mcp.errors import LimitError
from justpen_knowledgebase_mcp.status import DerivedStorage
from justpen_knowledgebase_mcp.storage.status import sample_derived_storage

from .helpers import cursor, database


def test_unsupported_dbstat_has_nullable_bytes_and_closes_schema_cursor():
    names = cursor(rows=[("search_documents",)])
    db = database(names, apsw.SQLError("no such table: private details"))
    result = DerivedStorage.model_validate(sample_derived_storage(db, Mock()))
    assert not result.available
    assert result.reason == "DBSTAT_UNAVAILABLE"
    assert result.text_projection_bytes is None
    assert result.fts_index_bytes is None
    assert result.property_index_bytes is None
    names.close.assert_called_once()


def test_page_stream_interrupt_closes_both_cursors_without_partial_total():
    names = cursor(rows=[("search_documents",)])
    pages = cursor(rows=[(4096,), (4096,)])
    token = Mock()
    token.check.side_effect = [None, LimitError("deadline")]
    with pytest.raises(LimitError):
        sample_derived_storage(database(names, pages), token)
    names.close.assert_called_once()
    pages.close.assert_called_once()
    assert token.check.call_count == 2
