import hashlib
from importlib import metadata
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

APSW_VERSION = "3.53.4.0"
APSW_STUB_SHA256 = "bb9c1f61f3a2d5f1e7128771b0807792cce66a48d1146251a1c6c6f4f5707a47"
PROJECT_ROOT = Path(__file__).parents[2]
VENDORED_STUB = PROJECT_ROOT / "typings" / "apsw" / "__init__.pyi"

ORIGINAL_IMPORTS = (
    b"import contextvars\n",
    b"from collections.abc import Mapping, Buffer, Iterator, Awaitable\n",
)
COMPATIBLE_IMPORTS = (
    b"import contextvars\nimport sys\n",
    b"from collections.abc import Mapping, Iterator, Awaitable\n\n"
    b"if sys.version_info >= (3, 12):\n"
    b"    from collections.abc import Buffer\n"
    b"else:\n"
    b"    from typing_extensions import Buffer\n",
)


def test_locked_apsw_stub_matches_typing_overlay() -> None:
    distribution = metadata.distribution("apsw")
    assert distribution.version == APSW_VERSION

    installed_path = Path(str(distribution.locate_file("apsw/__init__.pyi")))
    installed = installed_path.read_bytes()
    assert hashlib.sha256(installed).hexdigest() == APSW_STUB_SHA256
    assert all(installed.count(fragment) == 1 for fragment in ORIGINAL_IMPORTS)

    assert VENDORED_STUB.is_file(), "the locked APSW typing overlay is missing"
    expected = installed
    for original, compatible in zip(ORIGINAL_IMPORTS, COMPATIBLE_IMPORTS, strict=True):
        expected = expected.replace(original, compatible, 1)
    expected = expected.rstrip(b"\n") + b"\n"
    assert VENDORED_STUB.read_bytes() == expected
