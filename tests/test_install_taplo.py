"""The taplo installer must fail closed rather than install unverified bytes."""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("install_taplo", ROOT / "scripts/install_taplo.py")
assert _spec is not None
assert _spec.loader is not None
install_taplo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install_taplo)


def _serve(monkeypatch, payload: bytes) -> None:
    """Answer every download with `payload`, so no test reaches the network."""

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self) -> bytes:
            return payload

    monkeypatch.setattr(install_taplo.urllib.request, "urlopen", lambda *_, **__: Response())


@pytest.fixture
def aarch64(monkeypatch, tmp_path):
    monkeypatch.setattr(install_taplo.sys, "platform", "linux")
    monkeypatch.setattr(install_taplo.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(install_taplo, "_target", lambda: tmp_path / "taplo")
    return tmp_path / "taplo"


def test_a_platform_the_wheel_covers_installs_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(install_taplo.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(install_taplo, "_target", lambda: tmp_path / "taplo")
    assert install_taplo.main() == 0
    assert not (tmp_path / "taplo").exists()


def test_a_substituted_archive_is_refused(monkeypatch, aarch64, capsys):
    _serve(monkeypatch, gzip.compress(b"not taplo"))
    assert install_taplo.main() == 1
    assert "does not match the pinned" in capsys.readouterr().err
    assert not aarch64.exists()


def test_a_verified_archive_installs_an_executable(monkeypatch, aarch64):
    binary = b"#!/bin/sh\nexit 0\n"
    archive = gzip.compress(binary)
    monkeypatch.setattr(install_taplo, "ARCHIVE_SHA256", hashlib.sha256(archive).hexdigest())
    monkeypatch.setattr(install_taplo, "BINARY_SHA256", hashlib.sha256(binary).hexdigest())
    _serve(monkeypatch, archive)
    assert install_taplo.main() == 0
    assert aarch64.read_bytes() == binary
    assert aarch64.stat().st_mode & 0o777 == 0o700


def test_an_installed_binary_is_not_downloaded_again(monkeypatch, aarch64):
    binary = b"#!/bin/sh\nexit 0\n"
    monkeypatch.setattr(install_taplo, "BINARY_SHA256", hashlib.sha256(binary).hexdigest())
    aarch64.write_bytes(binary)
    monkeypatch.setattr(
        install_taplo.urllib.request,
        "urlopen",
        lambda *_, **__: pytest.fail("an installed binary must not be downloaded again"),
    )
    assert install_taplo.main() == 0


def test_the_target_is_the_directory_the_wheel_would_use():
    assert install_taplo._target().parent == Path(sys.executable).parent


def _locked_version(name: str) -> str:
    """Answer the version uv resolved for `name`, from its own package block."""
    packages = (ROOT / "uv.lock").read_text(encoding="utf-8").split("[[package]]")
    block = next(part for part in packages if f'\nname = "{name}"\n' in part)
    return next(line.split('"')[1] for line in block.splitlines() if line.startswith("version = "))


def test_the_pinned_version_matches_the_locked_wheel():
    """An aarch64 contributor must format with the taplo everyone else has."""
    assert _locked_version("taplo") == install_taplo.VERSION


def test_the_pinned_version_satisfies_the_declared_dependency():
    """pyproject.toml is the source of truth on every platform, aarch64 included."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = next(
        entry for entry in pyproject["dependency-groups"]["dev"] if isinstance(entry, str) and entry.startswith("taplo")
    )
    specifier = declared.split(";")[0].strip()
    assert SpecifierSet(specifier.removeprefix("taplo")).contains(Version(install_taplo.VERSION))
