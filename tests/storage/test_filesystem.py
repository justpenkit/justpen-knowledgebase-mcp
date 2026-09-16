"""Filesystem locality admission and real descriptor probe."""

import os

import pytest

from justpen_knowledgebase_mcp.errors import ConfigurationError
from justpen_knowledgebase_mcp.storage.filesystem import require_local, validate_local_directory


def test_network_and_unknown_mounts_fail_closed():
    for platform, kind, flags in [
        ("darwin", 0, 0),
        ("linux", 0x6969, 0),
        ("linux", 0xFF534D42, 0),
        ("linux", 0xDEAD, 0),
    ]:
        with pytest.raises(ConfigurationError, match="CONFIGURATION"):
            require_local(platform, kind, flags)


@pytest.mark.integration
def test_native_directory_reports_local(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        validate_local_directory(fd)
    finally:
        os.close(fd)
