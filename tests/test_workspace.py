"""Real descriptor containment and staging integration checks."""

import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.errors import ConfigurationError, InvalidParamsError, PathDeniedError, StorageIOError
from justpen_knowledgebase_mcp.workspace import WorkspacePaths

pytestmark = pytest.mark.integration


def workspace(root, **kwargs):

    return WorkspacePaths(ServerConfig(workspace_dir=root, **kwargs))


def test_missing_workspace(tmp_path):
    with pytest.raises(ConfigurationError, match="CONFIGURATION"):
        workspace(tmp_path / "missing")


@pytest.mark.parametrize("path", ["../outside", "/outside", "recon/../file"])
def test_reject_escape(tmp_path, path):
    with workspace(tmp_path) as ws, pytest.raises(PathDeniedError, match="PATH_DENIED"):
        ws.open_import(path).__enter__()


@pytest.mark.parametrize("inside", [True, False])
def test_symlink_component_even_if_target_is_inside(tmp_path, inside):
    target = tmp_path / "target" if inside else tmp_path.parent / (tmp_path.name + "-outside")
    target.mkdir()
    (target / "file").write_bytes(b"original")
    (tmp_path / "recon").symlink_to(target)
    with (
        workspace(tmp_path) as ws,
        pytest.raises(PathDeniedError, match="PATH_DENIED: SYMLINK_COMPONENT"),
        ws.open_import("recon/file"),
    ):
        pass


def test_alias_root_relative_and_absolute_inputs(tmp_path):
    alias = tmp_path.parent / (tmp_path.name + "-alias")
    alias.symlink_to(tmp_path)
    (tmp_path / "recon").mkdir()
    (tmp_path / "recon/nmap.xml").write_bytes(b"<nmap/>")
    with workspace(alias) as ws:
        for path in ["recon/nmap.xml", str(alias / "recon/nmap.xml"), str(tmp_path / "recon/nmap.xml")]:
            with ws.open_import(path) as fd:
                assert os.read(fd, 100) == b"<nmap/>"
        assert ws.root == tmp_path.resolve()
        assert stat.S_IMODE(ws.tmp.stat().st_mode) == 0o700


def test_hardlinks_fifo_and_managed_imports_denied(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"private")
    os.link(source, tmp_path / "alias")
    os.mkfifo(tmp_path / "fifo")
    with workspace(tmp_path) as ws:
        (ws.evidence / "file").write_bytes(b"evidence")
        for path in ["source", "alias", "fifo", str(ws.evidence / "file")]:
            with pytest.raises(PathDeniedError, match="PATH_DENIED"), ws.open_import(path):
                pass


def test_managed_override_escape_and_symlink(tmp_path):
    with pytest.raises(PathDeniedError, match="PATH_DENIED"):
        workspace(tmp_path, tmp_dir="../outside")
    (tmp_path / "alias").symlink_to(tmp_path)
    with pytest.raises(PathDeniedError, match="SYMLINK_COMPONENT"):
        workspace(tmp_path, data_dir="alias/data")


def test_import_detects_mutation(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"a")
    with workspace(tmp_path) as ws, pytest.raises(StorageIOError, match="SOURCE_CHANGED"), ws.open_import("source"):
        source.write_bytes(b"changed")


def test_stage_publish_uses_atomic_relative_paths(tmp_path):
    with workspace(tmp_path) as ws:
        with ws.stage() as (name, fd):
            os.write(fd, b"evidence")
            ws.publish(name, "ab/cd/digest")
        assert (ws.evidence / "ab/cd/digest").read_bytes() == b"evidence"
        assert not list(ws.tmp.iterdir())
        assert stat.S_IMODE((ws.evidence / "ab/cd/digest").stat().st_mode) == 0o600


def test_intermediate_directory_replaced_by_symlink_during_open(tmp_path, monkeypatch):
    recon = tmp_path / "recon"
    recon.mkdir()
    (recon / "file").write_bytes(b"original")
    real_open = os.open

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        if path == "recon":
            recon.rename(tmp_path / "moved")
            recon.symlink_to(tmp_path / "moved")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    with workspace(tmp_path) as ws:
        monkeypatch.setattr(os, "open", racing_open)
        with pytest.raises(PathDeniedError, match="SYMLINK_COMPONENT"), ws.open_import("recon/file"):
            pass


@pytest.mark.parametrize("root_spelling", ["/tmp", "/var"])  # noqa: S108 - exercise OS root alias with a unique TemporaryDirectory
def test_platform_root_alias_prefixes(tmp_path, root_spelling):
    if sys.platform != "darwin":
        pytest.skip("Darwin root aliases are not a portable filesystem layout")
    alias = Path(root_spelling)
    expected_root = Path("/private") / alias.name
    if not alias.is_symlink() or alias.resolve() != expected_root or not expected_root.is_dir():
        pytest.skip("Host does not expose the expected Darwin root alias")
    canonical = tmp_path.resolve()
    if root_spelling == "/tmp":  # noqa: S108 - exercise OS root alias with a unique TemporaryDirectory
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            root = Path(directory)
            (root / "recon").mkdir()
            (root / "recon/nmap.xml").write_bytes(b"xml")
            with workspace(Path("/tmp") / root.name) as ws, ws.open_import("recon/nmap.xml") as fd:  # noqa: S108 - exercise OS root alias with a unique TemporaryDirectory
                assert os.read(fd, 3) == b"xml"
    elif str(canonical).startswith("/private/var/"):
        (canonical / "recon").mkdir()
        (canonical / "recon/nmap.xml").write_bytes(b"xml")
        with workspace(Path(str(canonical).removeprefix("/private"))) as ws, ws.open_import("recon/nmap.xml") as fd:
            assert os.read(fd, 3) == b"xml"
    else:
        pytest.skip("Host does not expose /var→/private/var")


@pytest.mark.parametrize(("different", "denied"), [("tmp", True), ("db", False)])
def test_device_contract_uses_tmp_evidence_only(tmp_path, monkeypatch, different, denied):
    real_fstat = os.fstat
    target = tmp_path / different
    target.mkdir()
    identity = target.stat().st_ino

    def device_stat(fd):
        result = real_fstat(fd)
        if result.st_ino == identity:
            fields = list(result)
            fields[2] += 1
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(os, "fstat", device_stat)
    if denied:
        with pytest.raises(ConfigurationError, match="TMP_EVIDENCE_DEVICE_MISMATCH"):
            workspace(tmp_path, tmp_dir=target)
    else:
        with workspace(tmp_path, db_path=target / "graph.sqlite3"):
            pass


def test_empty_import_has_invalid_code(tmp_path):
    cfg = ServerConfig(workspace_dir=tmp_path)
    with WorkspacePaths(cfg) as workspace, pytest.raises(InvalidParamsError), workspace.open_import(""):
        pass


@pytest.mark.parametrize("path", ["nmap\x00.xml", "recon/nmap\x00.xml", "re\x00con/nmap.xml"])
def test_nul_byte_import_has_invalid_code(tmp_path, path):
    # os.open raises ValueError rather than OSError, so containment must reject
    # the spelling before any descriptor handler can be asked to classify it.
    with workspace(tmp_path) as ws:
        with pytest.raises(InvalidParamsError) as failure:
            ws.relative(path)
        assert failure.value.error_type == "INVALID"
        with pytest.raises(InvalidParamsError), ws.open_import(path):
            pass
