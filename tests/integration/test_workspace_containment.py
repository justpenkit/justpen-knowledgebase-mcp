"""Native cold-import audit. Missing OS tooling fails rather than silently skipping."""

import json
import os
import shutil
import subprocess
import sys

import pytest

from .native_trace import external_mutations

pytestmark = pytest.mark.integration


def audit_command(root, outside, scenario, trace):
    command = [sys.executable, "-B", "-m", "tests.integration.filesystem_probe", str(root), str(outside), scenario]
    if sys.platform == "darwin":
        binary = shutil.which("sandbox-exec")
        assert binary, "native audit requires macOS sandbox-exec"
        profile = (
            "(version 1)(allow default)(deny file-write*)"
            f"(allow file-write* (subpath {json.dumps(str(root))}))"
            '(allow file-write-data (literal "/dev/null") (literal "/dev/tty"))'
        )
        return [binary, "-p", profile, *command]
    assert sys.platform.startswith("linux"), "native audit supports Linux/macOS only"
    binary = shutil.which("strace")
    assert binary, "native audit requires strace installed in the runner"
    return [binary, "-f", "-yy", "-s", "4096", "-e", "trace=%file,write,pwrite64,ftruncate", "-o", str(trace), *command]


@pytest.mark.parametrize(
    "scenario",
    ["runtime", "denied-control", "telemetry-http", "telemetry-grpc", "removed-tmp", "unwritable-tmp", "recovery"],
)
def test_native_cold_import_and_spill_are_workspace_confined(tmp_path, scenario):
    root, outside = tmp_path / "workspace", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    trace = tmp_path / "trace.log"
    env = {key: value for key, value in os.environ.items() if not key.startswith(("OTEL_", "JUSTPEN_"))}
    env.update(
        PYTHONDONTWRITEBYTECODE="1",
        TMPDIR=str(outside),
        TEMP=str(outside),
        TMP=str(outside),
        XDG_CACHE_HOME=str(outside),
    )
    if scenario.startswith("telemetry"):
        env.update(
            JUSTPEN_SESSION_ID="native-audit",
            JUSTPEN_KNOWLEDGEBASE_OTEL_ENABLED="true",
            JUSTPEN_KNOWLEDGEBASE_OTEL_PROTOCOL="grpc" if scenario.endswith("grpc") else "http/protobuf",
            JUSTPEN_KNOWLEDGEBASE_OTEL_ENDPOINT="http://127.0.0.1:9",
            JUSTPEN_KNOWLEDGEBASE_OTEL_TIMEOUT="0.01",
            JUSTPEN_KNOWLEDGEBASE_OTEL_SHUTDOWN_TIMEOUT_MS="100",
            FASTMCP_TELEMETRY_MODE="native",
        )
    if sys.platform.startswith("linux") and scenario == "denied-control":
        binary = shutil.which("strace")
        assert binary, "native audit requires strace installed in the runner"
        command = [
            binary,
            "-f",
            "-yy",
            "-e",
            "trace=%file",
            "-o",
            str(trace),
            sys.executable,
            "-B",
            "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('control')",
            str(outside / "control"),
        ]
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30, check=False)
        assert result.returncode == 0, result.stderr
        assert external_mutations(trace.read_text(), root)
        return
    result = subprocess.run(
        audit_command(root, outside, scenario, trace), env=env, capture_output=True, text=True, timeout=90, check=False
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    if scenario == "denied-control":
        assert report["denied"] is True
    else:
        assert report["native_spills"] > 0
    assert list(outside.iterdir()) == []
    if trace.exists():
        violations = external_mutations(trace.read_text(), root)
        assert not violations, {"count": len(violations), "examples": violations[:5]}


def test_native_absolute_parent_traversal_positive_control(tmp_path):
    root, outside = tmp_path / "workspace", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "stage").write_bytes(b"stage")
    (outside / "victim").write_bytes(b"victim")
    trace = tmp_path / "trace.log"
    result = subprocess.run(
        audit_command(root, outside, "escaped-control", trace), capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    if sys.platform == "darwin":
        assert report["denied_operations"] == 3
        assert (outside / "victim").read_bytes() == b"victim"
        assert sorted(path.name for path in outside.iterdir()) == ["victim"]
    else:
        assert report["denied_operations"] == 0
        assert not (outside / "victim").exists()
        assert (outside / "renamed").read_bytes() == b"stage"
        assert (outside / "created").is_dir()
        violations = external_mutations(trace.read_text(), root)
        assert len(violations) == 3, violations
        assert all("/workspace/../outside/" in line for line in violations)
