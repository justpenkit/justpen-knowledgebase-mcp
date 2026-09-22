"""Install the taplo binary where the published wheel does not reach.

`pyproject.toml` declares `taplo` only where a wheel exists, which excludes
linux aarch64. Without it `make format-toml` fails with `FileNotFoundError`
rather than formatting, so this fetches the upstream release binary into the
environment's script directory, beside the wheel-installed entry points.

`VERSION` must match the version `uv.lock` resolves for the wheel platforms,
or an aarch64 contributor would format TOML with a different taplo than
everyone else and `make format-toml-check` would disagree across machines.
`tests/test_install_taplo.py` asserts that parity, so a lock bump fails
loudly instead of drifting.

Upstream publishes no checksum file, so the digests are pinned from a download
verified once. A release cannot be swapped underneath the pin without this
failing closed. They are not the wheel's own bytes: PyPI builds its own
binary, so only the version matches, not the file.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import platform
import stat
import sys
import sysconfig
import tempfile
import urllib.request
from pathlib import Path

VERSION = "0.9.3"  # keep equal to uv.lock; tests/test_install_taplo.py enforces it
ARCHIVE_SHA256 = "7c07379d3288fb5c26b1c29bbedec4f8d8f602d776bcc3a1578176733b6a857c"
BINARY_SHA256 = "1462a3a0fea21c61800854ddd43939a0ceb6170410ed0694ad601228902c2267"
URL = f"https://github.com/tamasfe/taplo/releases/download/{VERSION}/taplo-linux-aarch64.gz"


def _target() -> Path:
    """Answer the directory the wheel would have installed taplo into."""
    scripts = sysconfig.get_path("scripts")
    return Path(scripts or Path(sys.executable).parent) / "taplo"


def _needed() -> bool:
    """Answer whether this interpreter's platform is the one the wheel skips."""
    return sys.platform == "linux" and platform.machine() == "aarch64"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    """Install the pinned binary when this platform has no wheel to provide it."""
    if not _needed():
        print(f"taplo: wheel platform {sys.platform}/{platform.machine()}, nothing to install")
        return 0
    target = _target()
    if target.exists() and _digest(target.read_bytes()) == BINARY_SHA256:
        print(f"taplo: {VERSION} already installed at {target}")
        return 0
    print(f"taplo: fetching {VERSION} for linux/aarch64")
    with urllib.request.urlopen(URL, timeout=120) as response:
        archive = response.read()
    if (found := _digest(archive)) != ARCHIVE_SHA256:
        print(f"taplo: archive digest {found} does not match the pinned {ARCHIVE_SHA256}", file=sys.stderr)
        return 1
    binary = gzip.decompress(archive)
    if (found := _digest(binary)) != BINARY_SHA256:
        print(f"taplo: binary digest {found} does not match the pinned {BINARY_SHA256}", file=sys.stderr)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the target and rename, so a concurrent reader never sees a partial binary.
    handle, name = tempfile.mkstemp(dir=target.parent, prefix=".taplo-")
    staged = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(binary)
        # mkstemp already excludes group and other; add execute for the owner only.
        staged.chmod(stat.S_IRWXU)
        staged.replace(target)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    print(f"taplo: installed {VERSION} at {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
